import streamlit as st
import pandas as pd
import gspread
import datetime
from datetime import timedelta
import imaplib
import email
from bs4 import BeautifulSoup
import re
import streamlit.components.v1 as components
import plotly.express as px
from streamlit_autorefresh import st_autorefresh # <--- Import ตัวช่วย Auto Sync

# --- 1. SET PAGE CONFIG ---
st.set_page_config(page_title="Cloud OMS Dark", layout="wide", page_icon="🌙")

# --- CONFIGURATION ---
IMAP_SERVER = "imap.gmail.com"
SHEET_NAME = "MyOrderDB" 

# --- CONNECT TO GOOGLE SHEETS ---
def get_db_connection():
    try:
        if "gcp_service_account" not in st.secrets:
            st.error("❌ ไม่พบ Secrets 'gcp_service_account'")
            return None
        creds_dict = dict(st.secrets["gcp_service_account"])
        client = gspread.service_account_from_dict(creds_dict)
        sheet = client.open(SHEET_NAME)
        return sheet
    except Exception as e:
        st.error(f"🔥 เชื่อมต่อ Google Sheets ไม่ได้: {e}")
        return None

# --- DATABASE FUNCTIONS ---
def load_orders():
    sh = get_db_connection()
    if sh is None: return pd.DataFrame()
    try:
        worksheet = sh.worksheet("orders")
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        df.columns = df.columns.str.strip()
        required_cols = ['requested_date', 'remarks', 'total_qty', 'raw_html', 'status', 'total_amount']
        for col in required_cols:
            if col not in df.columns: df[col] = ""
        df.fillna("", inplace=True)
        return df
    except Exception as e:
        st.error(f"โหลดข้อมูลไม่ได้: {e}")
        return pd.DataFrame()

def save_new_order(order_data):
    sh = get_db_connection()
    if sh is None: return False
    try:
        ws = sh.worksheet("orders")
        try:
            cell = ws.find(str(order_data['order_id']))
            if cell: return False 
        except: pass 
        
        row = [
            str(order_data['order_id']),
            order_data['customer'],
            str(order_data['order_date']),
            str(order_data['delivery_datetime']),
            order_data['total_amount'],
            "Pending",
            order_data['raw_html'],
            order_data['requested_date'],
            order_data['remarks'],
            order_data['total_qty']
        ]
        ws.append_row(row)
        return True
    except Exception: return False

def update_status(order_ids, new_status):
    sh = get_db_connection()
    if sh is None: return
    try:
        ws = sh.worksheet("orders")
        log_ws = sh.worksheet("logs")
        if not isinstance(order_ids, list): order_ids = [order_ids]
        header = ws.row_values(1)
        status_col_idx = header.index('status') + 1
        for oid in order_ids:
            try:
                cell = ws.find(str(oid))
                if cell:
                    ws.update_cell(cell.row, status_col_idx, new_status)
                    log_ws.append_row([str(datetime.datetime.now()), str(oid), f"Updated to {new_status}"])
            except: pass
    except Exception as e:
        st.error(f"Update Error: {e}")

# --- CHECK URGENT ---
def check_urgent_status(req_date_str, remarks_str):
    if not req_date_str or not remarks_str: return False, "ไม่ลงข้อมูล", None
    try:
        date_obj = None
        for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
            try:
                date_obj = datetime.datetime.strptime(req_date_str.strip(), fmt).date()
                break
            except: continue
        if not date_obj: return False, "รูปแบบวันที่ผิด", None

        time_match = re.search(r"Delivery_Time.*?[:]\s*(\d{1,2}[.:]\d{2})", remarks_str, re.IGNORECASE)
        if not time_match: return False, "ไม่ลงข้อมูลเวลา", None
        
        time_str = time_match.group(1).replace(".", ":")
        time_obj = datetime.datetime.strptime(time_str, "%H:%M").time()
        delivery_dt = datetime.datetime.combine(date_obj, time_obj)
        now = datetime.datetime.now()
        diff = delivery_dt - now
        hours_left = diff.total_seconds() / 3600

        if 0 < hours_left <= 2:
            return True, f"🚨 ด่วน! ส่ง {time_str} (เหลือ {int(hours_left*60)} นาที)", delivery_dt
        elif hours_left <= 0:
             return False, f"⏰ เลยกำหนด ({time_str})", delivery_dt
        else:
            return False, f"ปกติ ส่ง {time_str}", delivery_dt
    except: return False, "Error", None

# --- EMAIL SYNC ---
def sync_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        if "email" not in st.secrets: return 0
        mail.login(st.secrets["email"]["user"], st.secrets["email"]["password"])
        mail.select("inbox")
        status, messages = mail.search(None, '(FROM "care@foodmarkethub.com")')
        email_ids = messages[0].split()
        new_count = 0
        for num in email_ids[-10:]:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                html_body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            html_body = part.get_payload(decode=True).decode()
                            break
                else:
                    html_body = msg.get_payload(decode=True).decode()

                if html_body:
                    soup = BeautifulSoup(html_body, "lxml")
                    text = soup.get_text(separator="\n")
                    order_match = re.search(r"(SO-\d+-\d+)", text)
                    if not order_match: continue
                    order_id = order_match.group(1)
                    
                    customer = "Unknown"
                    cust_match = re.search(r"received from\s(.*?)\swith order no", text)
                    if not cust_match: cust_match = re.search(r"(.*?)\shas placed a new order", text)
                    if cust_match: customer = cust_match.group(1).strip()
                    
                    total = 0.0
                    total_match = re.search(r"Total Amount\s*฿\s*([\d,]+\.?\d*)", text)
                    if total_match: total = float(re.sub(r'[^\d.]', '', total_match.group(1)))
                    
                    req_date = ""
                    req_match = re.search(r"Requested Delivery Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
                    if req_match: req_date = req_match.group(1).strip()
                    
                    remarks = ""
                    rem_pattern = re.search(r"Remarks:\s*([\s\S]*?)(?=\nNo\.|\nSKU|No\.SKU|Table)", text)
                    if rem_pattern: remarks = rem_pattern.group(1).strip()
                    else:
                        rem_fallback = re.search(r"Remarks:\s*(.*)", text)
                        if rem_fallback: remarks = rem_fallback.group(1).strip()

                    total_qty = 0
                    try:
                        tables = soup.find_all("table")
                        for table in tables:
                            headers = [th.get_text(strip=True).lower() for th in table.find_all(["th", "td"])]
                            if "qty" in headers and ("item" in headers or "sku" in headers):
                                qty_idx = headers.index("qty")
                                rows = table.find_all("tr")
                                for row in rows[1:]:
                                    cols = row.find_all("td")
                                    if len(cols) > qty_idx:
                                        clean_val = re.sub(r'[^\d.]', '', cols[qty_idx].get_text(strip=True))
                                        if clean_val and len(clean_val) < 5:
                                            total_qty += float(clean_val)
                                break 
                    except: pass
                    
                    delivery_dt = datetime.datetime.now() + timedelta(days=1) 
                    order_data = {
                        'order_id': order_id, 'customer': customer, 'order_date': datetime.date.today(),
                        'delivery_datetime': delivery_dt, 'total_amount': total, 'raw_html': html_body,
                        'requested_date': req_date, 'remarks': remarks, 'total_qty': total_qty
                    }
                    if save_new_order(order_data): new_count += 1
            except: continue
        mail.logout()
        return new_count
    except Exception: return 0

# --- AUTO SYNC LOGIC (5 MINUTES) ---
# บังคับรีเฟรชหน้าเว็บทุก 300,000 มิลลิวินาที (5 นาที)
# และเมื่อรีเฟรช ให้ทำการ Sync Emails ทันที
count = st_autorefresh(interval=5 * 60 * 1000, key="auto_sync_timer")

# --- MAIN UI ---
st.sidebar.title("🌙 Cloud OMS (Dark)")

# แสดงสถานะ Auto Sync
st.sidebar.write(f"🔄 Auto-Sync: ทำงานอยู่... (รอบที่ {count})")
st.sidebar.caption("ระบบจะดึงข้อมูลใหม่อัตโนมัติทุก 5 นาที")

# ปุ่ม Manual Sync (ยังเก็บไว้เผื่อใจร้อน)
if st.sidebar.button("⚡ กดเพื่อ Sync เดี๋ยวนี้", type="primary"):
    with st.spinner("กำลังดึงข้อมูล..."):
        cnt = sync_emails()
        if cnt > 0: st.sidebar.success(f"✅ เพิ่ม {cnt} ใบ")
        else: st.sidebar.info("ไม่มีรายการใหม่")
        st.rerun()

# ถ้าเป็นการ Auto Refresh ให้ลอง Sync ด้วย (แบบเงียบๆ)
if count > 0:
    sync_emails()

df = load_orders()

if not df.empty:
    try:
        df['total_amount'] = df['total_amount'].astype(str).str.replace(',', '', regex=True)
        df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
        df['total_qty'] = pd.to_numeric(df['total_qty'], errors='coerce').fillna(0)
    except: pass

    st.title("📊 Dashboard (Dark Mode)")
    
    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📦 Total Orders", f"{len(df)}")
    c2.metric("💰 Total Sales", f"฿{df['total_amount'].sum():,.0f}")
    c3.metric("🔢 Total Items", f"{df['total_qty'].sum():,.0f}")
    if 'status' in df.columns:
        c4.metric("⏳ Pending", len(df[df['status']=='Pending']), delta_color="inverse")
    
    st.divider()
    
    # Graphs (Dark Theme)
    g1, g2 = st.columns([2, 1])
    with g1:
        daily = df.groupby('order_date')['total_amount'].sum().reset_index()
        # ใช้ template='plotly_dark' เพื่อให้กราฟเป็นสีมืด
        fig = px.bar(daily, x='order_date', y='total_amount', title="ยอดขายรายวัน", 
                     template="plotly_dark", color_discrete_sequence=['#00CC96'])
        st.plotly_chart(fig, use_container_width=True)
    with g2:
        if 'status' in df.columns:
            status_c = df['status'].value_counts().reset_index()
            status_c.columns = ['Status', 'Count']
            fig2 = px.pie(status_c, values='Count', names='Status', title="สัดส่วนสถานะ", hole=0.4,
                          template="plotly_dark", color_discrete_sequence=px.colors.qualitative.Bold)
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    
    # --- Order Management ---
    tab1, tab2, tab3 = st.tabs(["📝 Pending & Alerts", "🚚 Shipped", "❌ Canceled"])
    
    def render_tab(status_filter, tab_key):
        if 'status' not in df.columns: return
        filtered_df = df[df['status'] == status_filter]
        
        if filtered_df.empty:
            st.info("ไม่มีรายการ")
            return

        df_show = filtered_df.copy()
        df_show.insert(0, "Select", False)
        
        # Alert Calc
        alert_info = []
        for _, row in df_show.iterrows():
            is_urgent, msg, _ = check_urgent_status(row.get('requested_date'), row.get('remarks'))
            alert_info.append(msg)
        df_show['Alert'] = alert_info

        edited_df = st.data_editor(
            df_show[['Select', 'Alert', 'order_id', 'customer', 'requested_date', 'total_qty', 'total_amount']],
            column_config={
                "Select": st.column_config.CheckboxColumn("เลือก", width="small"),
                "Alert": st.column_config.TextColumn("สถานะเวลา", width="medium"),
                "total_amount": st.column_config.NumberColumn("Total", format="฿ %.2f")
            },
            key=f"editor_{tab_key}",
            use_container_width=True,
            hide_index=True
        )
        
        c_act = st.columns([2, 1])
        with c_act[0]:
            new_st = st.selectbox("เปลี่ยนสถานะ:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
        with c_act[1]:
            if st.button(f"บันทึกสถานะ", key=f"btn_{tab_key}", type="primary"):
                ids = edited_df[edited_df.Select]['order_id'].tolist()
                if ids:
                    update_status(ids, new_st)
                    st.toast("บันทึกสำเร็จ!")
                    st.cache_data.clear()
                    st.rerun()

        st.write("---")
        for idx, row in filtered_df.iterrows():
            is_urgent, msg, _ = check_urgent_status(row.get('requested_date'), row.get('remarks'))
            icon = "🔥" if is_urgent else ("⚠️" if "ไม่ลงข้อมูล" in msg else "📄")
            
            header = f"{icon} {row['order_id']} | {msg} | Qty: {row.get('total_qty',0)} | ฿{row['total_amount']:,.0f}"
            if is_urgent: st.error(header)
            else: st.write(f"**{header}**")

            with st.expander("รายละเอียด"):
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown(f"**Req Date:** {row.get('requested_date')}")
                    st.markdown(f"**Remarks:** {row.get('remarks')}")
                with c2: st.info(f"**Status:** {msg}")
                
                html_content = str(row.get('raw_html', ''))
                if len(html_content) > 50: components.html(html_content, height=600, scrolling=True)
                else: st.warning("ไม่พบรูปบิล")

    with tab1: render_tab("Pending", "t1")
    with tab2: render_tab("Shipped", "t2")
    with tab3: render_tab("Canceled", "t3")

else:
    st.warning("⚠️ ไม่พบข้อมูล")