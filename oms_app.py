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
import plotly.express as px  # <--- เพิ่ม Library กราฟ

# --- 1. SET PAGE CONFIG ---
st.set_page_config(page_title="Cloud OMS", layout="wide", page_icon="☁️")

# --- CONFIGURATION ---
IMAP_SERVER = "imap.gmail.com"
SHEET_NAME = "MyOrderDB" 

# --- CONNECT TO GOOGLE SHEETS ---
def get_db_connection():
    try:
        if "gcp_service_account" not in st.secrets:
            st.error("ไม่พบ Secrets 'gcp_service_account'")
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
        
        # --- CLEANING DATA (จุดแก้สำคัญ) ---
        # 1. ลบช่องว่างหัวตาราง (เผื่อพิมพ์เว้นวรรคมา)
        df.columns = df.columns.str.strip()
        
        # 2. ถมค่าว่างด้วย string ว่าง (ป้องกัน Error NaN)
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
        except:
            pass 
        row = [
            str(order_data['order_id']),
            order_data['customer'],
            str(order_data['order_date']),
            str(order_data['delivery_datetime']),
            order_data['total_amount'],
            "Pending",
            order_data['raw_html']
        ]
        ws.append_row(row)
        return True
    except Exception:
        return False

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

# --- EMAIL FETCHING ---
def sync_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        if "email" not in st.secrets:
            st.error("ไม่พบ Secrets 'email'")
            return 0
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
                    text = soup.get_text()
                    
                    # Regex Pattern (Food Market Hub)
                    order_match = re.search(r"(SO-\d+-\d+)", text)
                    if not order_match: continue
                    order_id = order_match.group(1)
                    
                    customer = "Food Market Customer"
                    cust_match = re.search(r"received from\s(.*?)\swith order no", text)
                    if not cust_match:
                         cust_match = re.search(r"(.*?)\shas placed a new order", text)
                    if cust_match: customer = cust_match.group(1).strip()
                    
                    total = 0.0
                    total_match = re.search(r"Total Amount\s*฿\s*([\d,]+\.?\d*)", text)
                    if total_match: 
                        clean_num = re.sub(r'[^\d.]', '', total_match.group(1))
                        total = float(clean_num)
                    
                    delivery_dt = datetime.datetime.now() + timedelta(days=1) 
                    order_data = {
                        'order_id': order_id,
                        'customer': customer,
                        'order_date': datetime.date.today(),
                        'delivery_datetime': delivery_dt,
                        'total_amount': total,
                        'raw_html': html_body
                    }
                    if save_new_order(order_data):
                        new_count += 1
            except: continue
        mail.logout()
        return new_count
    except Exception:
        return 0

# --- MAIN UI ---
st.sidebar.title("☁️ Cloud Order System")

if st.sidebar.button("🔄 Sync & Refresh", type="primary"):
    with st.spinner("กำลังดึงข้อมูล..."):
        cnt = sync_emails()
        if cnt > 0: 
            st.sidebar.success(f"✅ เพิ่ม {cnt} ออเดอร์ใหม่")
            st.cache_data.clear()
        else: 
            st.sidebar.info("ยังไม่มีออเดอร์ใหม่")
        st.rerun()

# โหลดข้อมูล
df = load_orders()

if not df.empty:
    # --- Data Prep ---
    try:
        df['total_amount'] = df['total_amount'].astype(str).str.replace(',', '', regex=True)
        df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
        df['delivery_datetime'] = pd.to_datetime(df['delivery_datetime'], errors='coerce')
        # แปลงวันที่สั่งซื้อเพื่อให้พล็อตกราฟได้
        df['order_date'] = pd.to_datetime(df['order_date'], errors='coerce').dt.date
    except: pass

    # --- Dashboard Section ---
    st.title("📊 Dashboard (Online)")
    
    # 1. Metrics (การ์ดตัวเลข)
    total_sales = df['total_amount'].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("📦 Total Orders", f"{len(df)} ใบ")
    c2.metric("💰 Total Sales", f"฿{total_sales:,.2f}")
    if 'status' in df.columns:
        c3.metric("⏳ Pending", len(df[df['status']=='Pending']), delta_color="inverse")
    
    st.divider()

    # 2. Graphs (กราฟ)
    col_g1, col_g2 = st.columns([2, 1])
    
    with col_g1:
        # กราฟยอดขายรายวัน
        daily_sales = df.groupby('order_date')['total_amount'].sum().reset_index()
        fig_bar = px.bar(daily_sales, x='order_date', y='total_amount', 
                         title="📈 ยอดขายรายวัน (Daily Sales)", 
                         labels={'total_amount': 'ยอดขาย (บาท)', 'order_date': 'วันที่'},
                         color_discrete_sequence=['#FF9F36'])
        st.plotly_chart(fig_bar, use_container_width=True)
        
    with col_g2:
        # กราฟสัดส่วนสถานะ
        if 'status' in df.columns:
            status_counts = df['status'].value_counts().reset_index()
            status_counts.columns = ['Status', 'Count']
            fig_pie = px.pie(status_counts, values='Count', names='Status', 
                             title="🍰 สถานะออเดอร์",
                             hole=0.4,
                             color_discrete_sequence=px.colors.qualitative.Pastel)
            st.plotly_chart(fig_pie, use_container_width=True)

    st.divider()

    # --- Order Management Tabs ---
    tab1, tab2, tab3 = st.tabs(["📝 Pending", "🚚 Shipped", "❌ Canceled"])
    
    def render_tab(status_filter, tab_key):
        if 'status' not in df.columns: return
        
        filtered_df = df[df['status'] == status_filter]
        
        if filtered_df.empty:
            st.info("ไม่มีรายการ")
            return

        # ตารางรายการ
        st.write("##### ✅ เลือกรายการ:")
        df_show = filtered_df.copy()
        if 'total_amount' not in df_show.columns: df_show['total_amount'] = 0.0
        
        df_display = df_show[['order_id', 'customer', 'total_amount']].reset_index(drop=True)
        df_display.insert(0, "Select", False)
        
        edited_df = st.data_editor(
            df_display,
            column_config={
                "Select": st.column_config.CheckboxColumn("เลือก", default=False, width="small"),
                "total_amount": st.column_config.NumberColumn("ยอดเงิน", format="฿ %.2f")
            },
            key=f"editor_{tab_key}",
            use_container_width=True,
            hide_index=True
        )
        
        # ปุ่มดำเนินการ (อยู่นอก Loop)
        col_act = st.columns([2, 1])
        with col_act[0]:
            new_st = st.selectbox("เปลี่ยนสถานะ:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
        with col_act[1]:
            if st.button(f"บันทึก ({tab_key})", key=f"btn_{tab_key}", type="primary"):
                ids = edited_df[edited_df.Select]['order_id'].tolist()
                if ids:
                    update_status(ids, new_st)
                    st.toast("บันทึกสำเร็จ!")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.toast("กรุณาเลือกรายการ")

        # การ์ดแสดงรายละเอียด
        st.divider()
        st.caption("คลิกเพื่อดูบิล")
        for idx, row in filtered_df.iterrows():
            # เช็คว่ามีข้อมูลบิลหรือไม่ (แก้ Logic ให้แม่นยำขึ้น)
            raw_html_content = str(row.get('raw_html', '')).strip()
            has_bill = len(raw_html_content) > 20 # ถ้ามีตัวอักษรเกิน 20 ตัว ถือว่ามีบิล
            
            icon = "📄" if has_bill else "⚠️"
            
            with st.expander(f"{icon} {row['order_id']} | {row['customer']} | ฿{row['total_amount']:,.2f}"):
                if has_bill:
                    components.html(raw_html_content, height=600, scrolling=True)
                else:
                    st.warning("ไม่พบโค้ดบิล (HTML) ในฐานข้อมูล")
                    # Debug: แสดงข้อมูลดิบเผื่อเช็ค
                    with st.expander("ดูข้อมูลดิบ (สำหรับตรวจสอบ)"):
                        st.code(raw_html_content)

    # Render Tabs
    with tab1: render_tab("Pending", "t1")
    with tab2: render_tab("Shipped", "t2")
    with tab3: render_tab("Canceled", "t3")
    
    # --- Debug Helper (ซ่อนอยู่ล่างสุด) ---
    with st.expander("🛠️ Debug Data (สำหรับนักพัฒนา)"):
        st.write("นี่คือข้อมูล 5 แถวแรกที่โปรแกรมอ่านได้จาก Google Sheet:")
        st.dataframe(df.head())
        st.write("รายชื่อคอลัมน์:", df.columns.tolist())

else:
    st.warning("⚠️ ไม่พบข้อมูล หรือเชื่อมต่อ Google Sheets ไม่ได้")
    st.info("ลองกดปุ่ม Sync ด้านซ้ายมือเพื่อดึงข้อมูลใหม่")