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
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pytz
from sqlalchemy.sql import text

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
UPDATE_TIME = time(19, 0)
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI BAĞLANTISI ---
try:
    conn = st.connection("supabase", type="sql")
except Exception as e:
    st.error(f"Veritabanı bağlantısı kurulamadı. 'Secrets' ayarlarınızı kontrol edin. Hata: {e}")
    st.stop()
    
# --- Abone ve E-posta Kayıt Yönetimi Fonksiyonları (Yeniden Yazıldı ve Sağlamlaştırıldı) ---
@st.cache_data(ttl=60, show_spinner=False)
def get_subscribers():
    df = conn.query('SELECT email FROM subscribers', show_spinner=False)
    return df['email'].tolist()

def add_subscriber(email):
    clean_email = email.strip().lower()
    try:
        with conn.session as s:
            query = text("INSERT INTO subscribers (email) VALUES (:email) ON CONFLICT (email) DO NOTHING;")
            result = s.execute(query, params={"email": clean_email})
            s.commit()
        get_subscribers.clear()
        if result.rowcount > 0:
            st.success(f"{clean_email} abone listesine başarıyla eklendi!")
        else:
            st.warning("Bu e-posta adresi zaten listede.")
    except Exception as e:
        st.error(f"Veritabanı hatası: {e}")

def remove_subscriber(email):
    clean_email = email.strip().lower()
    try:
        with conn.session as s:
            query = text("DELETE FROM subscribers WHERE email = :email;")
            result = s.execute(query, params={"email": clean_email})
            s.commit()
        get_subscribers.clear()
        if result.rowcount > 0:
            st.success(f"{clean_email} listeden başarıyla çıkarıldı.")
        else:
            st.warning("Bu e-posta adresi listede bulunamadı.")
    except Exception as e:
        st.error(f"Veritabanı hatası: {e}")

@st.cache_data(ttl=60, show_spinner=False)
def get_last_email_sent_info():
    try:
        query = "SELECT MAX(sent_at) as last_sent FROM sent_emails"
        df = conn.query(query, show_spinner=False)
        if not df.empty and pd.notna(df['last_sent'].iloc[0]):
            last_sent_utc = df['last_sent'].iloc[0].replace(tzinfo=pytz.UTC)
            return last_sent_utc.astimezone(TIMEZONE)
        return None
    except Exception as e:
        print(f"Son e-posta zamanı çekilemedi: {e}")
        return None

def check_if_email_sent(cache_key):
    try:
        query = "SELECT COUNT(*) FROM sent_emails WHERE cache_key = :cache_key"
        df = conn.query(query, params={"cache_key": cache_key}, show_spinner=False, ttl=0)
        return df.iloc[0, 0] > 0
    except Exception as e:
        st.sidebar.warning(f"Gönderilmiş e-posta kontrol edilemedi: {e}")
        return True

def log_email_sent(cache_key):
    try:
        with conn.session as s:
            s.execute(text("INSERT INTO sent_emails (cache_key) VALUES (:cache_key);"), params={"cache_key": cache_key})
            s.commit()
        get_last_email_sent_info.clear()
    except Exception as e:
        st.sidebar.error(f"E-posta gönderim kaydı yapılamadı: {e}")

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
    except Exception as e:
        return False, f"Bilinmeyen bir hata oluştu: {e}"

# --- VERİ İŞLEME FONKSİYONLARI ---
def fetch_stock_tickers(url, headers):
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table_rows = soup.find("div", {"class": "single-table"}).tbody.findAll("tr")
        return [row.a.text.strip() for row in table_rows]
    except requests.exceptions.ConnectTimeout:
        st.error("Hisse senedi listesi çekilirken sunucuya bağlanılamadı (Timeout). Sunucu, bu uygulamadan gelen istekleri engelliyor olabilir. Lütfen daha sonra 'Verileri Yeniden Yükle' butonu ile tekrar deneyin.")
        return []
    except requests.exceptions.RequestException as e:
        st.error(f"Hisse senedi listesi çekilirken bir ağ hatası oluştu: {e}")
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

@st.cache_data(show_spinner=False)
def run_full_analysis(_cache_key):
    stock_tickers = fetch_stock_tickers(CONFIG["isyatirim_url"], CONFIG["headers"])
    if not stock_tickers: return None
    all_stock_data = {}
    
    progress_bar_container = st.empty()
    
    async def run_fetch():
        timeout = aiohttp.ClientTimeout(total=60)
        semaphore = asyncio.Semaphore(CONFIG["concurrent_requests"])
        async with aiohttp.ClientSession(headers=CONFIG["headers"], timeout=timeout) as session:
            tasks = [asyncio.ensure_future(fetch_stock_data(session, stock, semaphore)) for stock in stock_tickers]
            results = []
            total_stocks, processed_stocks = len(stock_tickers), 0
            for f in asyncio.as_completed(tasks):
                result = await f
                results.append(result)
                processed_stocks += 1
                progress_bar_container.progress(processed_stocks / total_stocks, text=f"Piyasa verileri çekiliyor... ({processed_stocks}/{total_stocks})")
            return results

    results = asyncio.run(run_fetch())
    progress_bar_container.empty()
    
    for stock_code, raw_data in results:
        if raw_data:
            df = process_raw_data(raw_data)
            df = clean_data(df)
            df = calculate_indicators(df)
            all_stock_data[stock_code] = df
    
    tum_hisseler_df = generate_summary_df(all_stock_data, stock_tickers)
    firsat_df = tum_hisseler_df[tum_hisseler_df['Muhind'] < CONFIG["muhind_filter_value"]]
    portfoy_df = generate_summary_df(all_stock_data, CONFIG["portfolio"])
    st.success(f"Veriler {datetime.now(TIMEZONE).strftime('%d-%m-%Y %H:%M:%S')} (TSİ) itibarıyla başarıyla güncellendi!")
    
    return {"firsat_df": firsat_df, "tum_hisseler_df": tum_hisseler_df, "portfoy_df": portfoy_df, "all_stock_data": all_stock_data}

# --- ANA MANTIK FONKSİYONU ---
def main():
    st.set_page_config(page_title="Hisse Analiz Aracı", layout="wide")
    
    st.title("📈 Otomatik BİST Hisse Senedi Analiz Aracı")
    st.markdown("Bu araç, her gün **Türkiye saatiyle 19:00'dan** sonraki ilk ziyarette BİST verilerini otomatik olarak günceller.")

    now = datetime.now(TIMEZONE)
    
    with st.sidebar:
        st.header("⚙️ Veri Kontrolü")
        if st.button("🔄 Verileri Yeniden Yükle"):
            st.cache_data.clear()
            st.rerun()

        st.divider()
        st.header("🔍 Fırsat Filtreleri")
        muhind_max = st.slider("Maksimum Muhind Değeri:", 0.5, 2.0, 0.9, 0.05)
        rsi_max = st.slider("Maksimum RSI Değeri:", 1, 100, 30, 1)
        p_ema_max = st.slider("Maksimum Fiyat/EMA200 Oranı:", 0.5, 2.0, 0.75, 0.05)


    cache_key = now.date().isoformat()
    if now.time() >= UPDATE_TIME:
        cache_key += "-aksam"
    
    analysis_results = run_full_analysis(cache_key)

    with st.sidebar:
        st.divider()
        st.header("🔔 E-posta Aboneliği")
        email_input = st.text_input("E-posta Adresiniz:", placeholder="ornek@gmail.com", key="email_input_key")

        def subscribe_action():
            email = st.session_state.email_input_key
            if "@" in email and "." in email:
                add_subscriber(email)
            else:
                st.error("Lütfen geçerli bir e-posta adresi girin.")

        def unsubscribe_action():
            email = st.session_state.email_input_key
            if "@" in email and "." in email:
                remove_subscriber(email)
            else:
                st.error("Lütfen geçerli bir e-posta adresi girin.")
        
        st.button("Abone Ol", on_click=subscribe_action)
        st.button("Abonelikten Çık", on_click=unsubscribe_action)
        
        st.divider()
        st.header("⚙️ E-posta Test")
        if st.button("Test E-postası Gönder"):
            email = st.session_state.email_input_key
            if "@" in email and "." in email:
                with st.spinner("Test e-postası gönderiliyor..."):
                    success, message = send_email(email, "Test E-postası", "<html><body>Bu bir test mesajıdır.</body></html>")
                    if success: st.success(f"Başarılı! '{email}' adresine test e-postası gönderildi.")
                    else: st.error(f"Başarısız! Hata: {message}")
            else:
                st.warning("Lütfen test e-postası göndermek için geçerli bir e-posta adresi girin.")
        
        if analysis_results:
            st.divider()
            st.header("📊 Uygulama Durumu")
            st.write(f"**Sunucu Saati (TSİ):** {now.strftime('%H:%M:%S')}")
            st.write(f"**Önbellek Anahtarı:**")
            st.code(cache_key)
            last_sent_time = get_last_email_sent_info()
            st.write(f"**Son Bildirim:**")
            st.code(last_sent_time.strftime('%d-%m-%Y %H:%M') if last_sent_time else "Henüz Yok")

    if analysis_results:
        tum_hisseler_df = analysis_results["tum_hisseler_df"]
        portfoy_df = analysis_results["portfoy_df"]
        all_stock_data = analysis_results["all_stock_data"]
        
        tab1, tab2, tab3, tab4 = st.tabs(["📊 İnteraktif Fırsat Tarama", "🗂️ Tüm Hisseler", "💼 Portföyüm", "🔍 Hisse Detay"])
        with tab1:
            st.header("İnteraktif Fırsat Tarama")
            filtered_df = tum_hisseler_df[
                (tum_hisseler_df['Muhind'] <= muhind_max) &
                (tum_hisseler_df['Rsi'] <= rsi_max) &
                (tum_hisseler_df['P/Ema200'] <= p_ema_max)
            ]
            st.dataframe(filtered_df)

        with tab2: st.header("Tüm Hisselerin Analizi"); st.dataframe(tum_hisseler_df)
        with tab3: st.header("Portföyümdeki Hisselerin Durumu"); st.dataframe(portfoy_df)
        with tab4:
            st.header("Detaylı Hisse İnceleme")
            stock_list = sorted(all_stock_data.keys())
            selected_stock = st.selectbox("İncelemek istediğiniz hisseyi seçin:", stock_list)
            if selected_stock:
                df_detail = all_stock_data[selected_stock]
                st.subheader(f"{selected_stock} - Güncel Değerler"); st.dataframe(tum_hisseler_df[tum_hisseler_df['Hisse'] == selected_stock])
                st.subheader(f"{selected_stock} - Fiyat Grafiği"); st.line_chart(df_detail.set_index('Tarih')['Fiyat'])
                st.subheader(f"{selected_stock} - Muhind İndikatör Grafiği"); st.line_chart(df_detail.set_index('Tarih')['muhind'])

        if not check_if_email_sent(cache_key):
            firsat_df_default = analysis_results["firsat_df"]
            firsat_hisseleri_listesi = firsat_df_default['Hisse'].tolist() if not firsat_df_default.empty else []
            subscribers = get_subscribers()
            if firsat_hisseleri_listesi and subscribers and now.time() >= UPDATE_TIME:
                st.sidebar.info(f"{len(subscribers)} aboneye e-posta gönderiliyor...")
                email_body_html = f"<html><body><p>Günün Hisse Fırsatları:</p><ul>{''.join([f'<li><b>{s}</b></li>' for s in firsat_hisseleri_listesi])}</ul></body></html>"
                subject = "Günlük Hisse Fırsatları Raporu"
                success_count = 0
                for sub in subscribers:
                    success, message = send_email(sub, subject, email_body_html)
                    if success: success_count += 1
                
                st.sidebar.success(f"{success_count}/{len(subscribers)} aboneye bildirim gönderildi.")
                if success_count > 0:
                    log_email_sent(cache_key)

if __name__ == "__main__":
    main()

