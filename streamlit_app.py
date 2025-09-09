import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from datetime import datetime, time, timedelta
import asyncio
import aiohttp
import nest_asyncio
import pickle
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pytz

# Jupyter/Spyder gibi ortamlarda asyncio hatasını önlemek için
nest_asyncio.apply()

# --- Konfigürasyon ---
CONFIG = {
    "isyatirim_url": "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/default.aspx",
    "data_url_template": "https://www.isyatirim.com.tr/_Layouts/15/IsYatirim.Website/Common/ChartData.aspx/IndexHistoricalAll?period=1440&from={from_date}&to={to_date}&endeks={stock_code}",
    "start_date": "20200101000000",
    "end_date": "20251231235959",
    "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/5.0 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/5.0"},
    "max_data_rows": 4108, "ema_period": 200, "rsi_period": 14, "muhind_filter_value": 0.9,
    "portfolio": ["MHRGY", "RTALB", "ALKA", "KLSER", "EUREN", "DOAS", "CVKMD", "IHAAS", "IZENR"],
    "concurrent_requests": 10, "request_delay": 0.1
}

# --- Ayarlar ---
CACHE_FILE = "data_cache.pkl"
UPDATE_TIME = time(19, 0)
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI BAĞLANTISI ---
try:
    conn = st.connection("supabase", type="sql")
except Exception as e:
    st.error(f"Veritabanı bağlantısı kurulamadı. 'Secrets' ayarlarınızı kontrol edin. Hata: {e}")
    st.stop()
    
# --- Abone Yönetimi Fonksiyonları ---
def get_subscribers():
    """Abone listesini veritabanından okur."""
    # show_spinner=False ile "Running sql.query..." yazısını gizliyoruz.
    df = conn.query('SELECT email FROM subscribers', ttl=0, show_spinner=False)
    return df['email'].tolist()

def add_subscriber(email):
    """Veritabanına yeni bir abone ekler."""
    if email not in get_subscribers():
        with conn.session as s:
            s.execute(f"INSERT INTO subscribers (email) VALUES ('{email}');")
            s.commit()
        return True
    return False

def remove_subscriber(email):
    """Veritabanından bir aboneyi siler."""
    if email in get_subscribers():
        with conn.session as s:
            s.execute(f"DELETE FROM subscribers WHERE email = '{email}';")
            s.commit()
        return True
    return False

# --- E-POSTA GÖNDERME FONKSİYONU ---
def send_email(recipient_email, subject, html_body):
    try:
        sender_email = st.secrets["email_credentials"]["SENDER_EMAIL"]
        sender_password = st.secrets["email_credentials"]["SENDER_PASSWORD"]
        smtp_server = st.secrets["email_credentials"]["SMTP_SERVER"]
        smtp_port = st.secrets["email_credentials"]["SMTP_PORT"]

        message = MIMEMultipart("alternative")
        message["From"] = sender_email
        message["To"] = recipient_email
        message["Subject"] = subject
        message.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient_email, message.as_string())
        return True, "E-posta başarıyla gönderildi."
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP Kimlik Doğrulama Hatası! Lütfen 'Secrets' içindeki SENDER_EMAIL veya SENDER_PASSWORD (Uygulama Şifresi) bilgilerinizi kontrol edin."
    except Exception as e:
        return False, f"Bilinmeyen bir hata oluştu: {e}"

# --- VERİ İŞLEME FONKSİYONLARI ---
def fetch_stock_tickers(url, headers):
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table_rows = soup.find("div", {"class": "single-table"}).tbody.findAll("tr")
        return [row.a.text.strip() for row in table_rows]
    except requests.exceptions.RequestException as e:
        st.error(f"Hisse senedi listesi çekilirken hata oluştu: {e}")
        return []
async def fetch_stock_data(session, stock_code, semaphore):
    url = CONFIG["data_url_template"].format(from_date=CONFIG["start_date"], to_date=CONFIG["end_date"], stock_code=stock_code)
    async with semaphore:
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                await asyncio.sleep(CONFIG["request_delay"])
                return stock_code, data.get("data", [])
        except Exception:
            return stock_code, None
def process_raw_data(raw_data):
    if not raw_data: return pd.DataFrame()
    dates = pd.to_datetime([item[0] for item in raw_data], unit='ms')
    prices = [item[1] for item in raw_data]
    return pd.DataFrame({"Tarih": dates, "Fiyat": prices})
def clean_data(df):
    if len(df) > CONFIG["max_data_rows"]:
        df = df.iloc[-CONFIG["max_data_rows"]:].reset_index(drop=True)
    df['Fiyat'] = df['Fiyat'].replace(0, np.nan).replace(0.0001, np.nan).ffill().bfill()
    return df
def calculate_indicators(df):
    if 'Fiyat' not in df.columns or df['Fiyat'].isnull().all() or len(df) < CONFIG["ema_period"]:
        return df
    df["ema200"] = EMAIndicator(df["Fiyat"], window=CONFIG["ema_period"]).ema_indicator()
    df["rsi"] = RSIIndicator(df["Fiyat"], window=CONFIG["rsi_period"]).rsi()
    df["p/ema200"] = df["Fiyat"] / df["ema200"]
    df["ema200ort"] = EMAIndicator(df["p/ema200"], window=CONFIG["ema_period"]).ema_indicator()
    df["muhind"] = df["p/ema200"] / df["ema200ort"]
    df['Degisim'] = round((df['Fiyat'] / df['Fiyat'].shift(1) - 1) * 100, 2)
    return df
def generate_summary_df(stock_data_dict, stock_list):
    summary_data = []
    for stock in stock_list:
        if stock in stock_data_dict and not stock_data_dict[stock].empty and "muhind" in stock_data_dict[stock].columns:
            df = stock_data_dict[stock]
            last_row = df.iloc[-1]
            lookback_period = min(240, len(df))
            summary_data.append({"Hisse": stock, "Fiyat": last_row.get("Fiyat"), "Degisim": last_row.get("Degisim"), "Rsi": last_row.get("rsi"), "Ema200": last_row.get("ema200"), "P/Ema200": last_row.get("p/ema200"), "Ema200Ort": last_row.get("ema200ort"), "Muhind": last_row.get("muhind"), "LowestMuhind": df['muhind'].iloc[-lookback_period:].min(), "HighestMuhind": df['muhind'].iloc[-lookback_period:].max()})
    return pd.DataFrame(summary_data)

# Dosya bazlı önbelleğe güvendiğimiz için @st.cache_data'yı kaldırıyoruz.
def run_full_analysis():
    stock_tickers = fetch_stock_tickers(CONFIG["isyatirim_url"], CONFIG["headers"])
    if not stock_tickers: return None, None, None, None
    all_stock_data = {}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    progress_bar_container = st.empty()
    progress_bar_container.progress(0, text="Hisse verileri çekiliyor...")
    async def run_fetch():
        semaphore = asyncio.Semaphore(CONFIG["concurrent_requests"])
        total_stocks, processed_stocks = len(stock_tickers), 0
        async with aiohttp.ClientSession(headers=CONFIG["headers"]) as session:
            tasks = [asyncio.ensure_future(fetch_stock_data(session, stock, semaphore)) for stock in stock_tickers]
            results = []
            for f in asyncio.as_completed(tasks):
                result = await f
                results.append(result)
                processed_stocks += 1
                progress_bar_container.progress(processed_stocks / total_stocks, text=f"Hisse verileri çekiliyor... ({processed_stocks}/{total_stocks})")
            return results
    results = loop.run_until_complete(run_fetch())
    progress_bar_container.empty()
    for stock_code, raw_data in results:
        if raw_data:
            df = process_raw_data(raw_data)
            df = clean_data(df)
            df = calculate_indicators(df)
            all_stock_data[stock_code] = df
    firsat_stocks = [ stock for stock, df in all_stock_data.items() if not df.empty and "muhind" in df.columns and df.iloc[-1]["muhind"] < CONFIG["muhind_filter_value"] ]
    firsat_df = generate_summary_df(all_stock_data, firsat_stocks)
    tum_hisseler_df = generate_summary_df(all_stock_data, stock_tickers)
    portfoy_df = generate_summary_df(all_stock_data, CONFIG["portfolio"])
    return firsat_df, tum_hisseler_df, portfoy_df, all_stock_data

# --- ANA MANTIK FONKSİYONU ---
def get_or_update_data():
    now = datetime.now(TIMEZONE)
    needs_update = True
    cached_data = None
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f: cached_data = pickle.load(f)
            cached_timestamp = cached_data.get("timestamp")
            if cached_timestamp: cached_timestamp = TIMEZONE.localize(cached_timestamp)
            if cached_timestamp and cached_timestamp.date() == now.date() and now.time() < UPDATE_TIME:
                needs_update = False
        except (pickle.UnpicklingError, EOFError, TypeError):
            st.warning("Önbellek dosyası bozuk, veriler yeniden çekilecek.")

    if not needs_update and cached_data:
        st.info(f"Veriler en son {cached_data['timestamp'].strftime('%d-%m-%Y %H:%M:%S')} (TSİ) tarihinde güncellenmiştir.")
        return cached_data

    with st.spinner("Piyasa verileri çekiliyor ve analiz ediliyor..."):
        firsat_df, tum_hisseler_df, portfoy_df, all_stock_data = run_full_analysis()
        if tum_hisseler_df is not None:
            firsat_hisseleri_listesi = firsat_df['Hisse'].tolist() if not firsat_df.empty else []
            subscribers = get_subscribers()
            if firsat_hisseleri_listesi and subscribers:
                st.sidebar.info("Fırsatlar bulundu! E-postalar gönderiliyor...")
                email_body_html = f"<html><body><p>Merhaba,</p><p>Günün Hisse Fırsatları Raporu:</p><ul>{''.join([f'<li><b>{stock}</b></li>' for stock in firsat_hisseleri_listesi])}</ul><p>İyi günler dileriz.</p></body></html>"
                subject = "Günlük Hisse Senedi Fırsatları Raporu"
                success_count = 0
                for sub in subscribers:
                    success, message = send_email(sub, subject, email_body_html)
                    if success: success_count += 1
                st.sidebar.success(f"{success_count}/{len(subscribers)} aboneye bildirim gönderildi.")

            new_data = {"firsat_df": firsat_df, "tum_hisseler_df": tum_hisseler_df, "portfoy_df": portfoy_df, "all_stock_data": all_stock_data, "timestamp": datetime.now()}
            with open(CACHE_FILE, "wb") as f: pickle.dump(new_data, f)
            st.success(f"Veriler {datetime.now(TIMEZONE).strftime('%d-%m-%Y %H:%M:%S')} (TSİ) itibarıyla başarıyla güncellendi!")
            return new_data
        else:
            st.error("Veri çekme sırasında bir hata oluştu.")
            return None

def to_csv(df): return df.to_csv(index=False).encode('utf-8')

# --- STREAMLIT ARAYÜZÜ ---
st.set_page_config(page_title="Hisse Analiz Aracı", layout="wide")

with st.sidebar:
    st.header("🔔 E-posta Aboneliği")
    email_input = st.text_input("E-posta Adresiniz:", placeholder="ornek@gmail.com")
    if st.button("Abone Ol"):
        if "@" in email_input and "." in email_input:
            if add_subscriber(email_input): st.success(f"{email_input} abone listesine eklendi!")
            else: st.warning("Bu e-posta adresi zaten listede.")
        else: st.error("Lütfen geçerli bir e-posta adresi girin.")
    if st.button("Abonelikten Çık"):
        if "@" in email_input and "." in email_input:
            if remove_subscriber(email_input): st.success(f"{email_input} listeden çıkarıldı.")
            else: st.warning("Bu e-posta adresi listede bulunamadı.")
        else: st.error("Lütfen geçerli bir e-posta adresi girin.")

    st.divider()
    st.header("⚙️ E-posta Test")
    if st.button("Test E-postası Gönder"):
        if "@" in email_input and "." in email_input:
            st.info("Test e-postası gönderiliyor...")
            test_subject = "Test E-postası | Hisse Analiz Aracı"
            test_body = "<html><body><p>Bu, e-posta ayarlarınızın doğru çalıştığını kontrol etmek için gönderilmiş bir test mesajıdır.</p></body></html>"
            success, message = send_email(email_input, test_subject, test_body)
            if success:
                st.success(f"Başarılı! '{email_input}' adresine test e-postası gönderildi.")
            else:
                st.error(f"Gönderim Başarısız! Hata: {message}")
        else:
            st.warning("Lütfen test e-postası göndermek için geçerli bir e-posta adresi girin.")

st.title("📈 Otomatik BİST Hisse Senedi Analiz Aracı")
st.markdown("Bu araç, her gün **Türkiye saatiyle 19:00'dan** sonraki ilk ziyarette BİST verilerini otomatik olarak günceller ve tüm abonelere yeni fırsatları e-posta ile bildirir.")

data = get_or_update_data()

if data:
    tab1, tab2, tab3, tab4 = st.tabs(["📊 Potansiyel Fırsatlar", "🗂️ Tüm Hisseler", "💼 Portföyüm", "🔍 Hisse Detay"])
    with tab1:
        st.header("Potansiyel Fırsatlar (`Muhind < 0.9`)")
        st.dataframe(data['firsat_df'])
        st.download_button("⬇️ Fırsatları CSV Olarak İndir", to_csv(data['firsat_df']), 'firsat_hisseleri.csv', 'text/csv')
    with tab2:
        st.header("Tüm Hisselerin Analizi")
        st.dataframe(data['tum_hisseler_df'])
        st.download_button("⬇️ Tümünü CSV Olarak İndir", to_csv(data['tum_hisseler_df']), 'tum_hisseler.csv', 'text/csv')
    with tab3:
        st.header("Portföyümdeki Hisselerin Durumu")
        st.dataframe(data['portfoy_df'])
        st.download_button("⬇️ Portföyü CSV Olarak İndir", to_csv(data['portfoy_df']), 'portfoy.csv', 'text/csv')
    with tab4:
        st.header("Detaylı Hisse İnceleme")
        stock_list = sorted(data['all_stock_data'].keys())
        selected_stock = st.selectbox("İncelemek istediğiniz hisseyi seçin:", stock_list)
        if selected_stock:
            df_detail = data['all_stock_data'][selected_stock]
            st.subheader(f"{selected_stock} - Güncel Değerler")
            st.dataframe(data['tum_hisseler_df'][data['tum_hisseler_df']['Hisse'] == selected_stock])
            st.subheader(f"{selected_stock} - Fiyat Grafiği")
            st.line_chart(df_detail.set_index('Tarih')['Fiyat'])
            st.subheader(f"{selected_stock} - Muhind İndikatör Grafiği")
            st.line_chart(df_detail.set_index('Tarih')['muhind'])

