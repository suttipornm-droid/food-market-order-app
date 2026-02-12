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

# --- 1. SET PAGE CONFIG (บรรทัดแรกสุด) ---
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
        return df
    except Exception as e:
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
                    order_match = re.search(r"(SO-\d+-\d+)", text)
                    if not order_match: continue
                    order_id = order_match.group(1)
                    
                    customer = "Food Market Customer"
                    cust_match = re.search(r"received from\s(.*?)\swith order no", text)
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

df = load_orders()

if not df.empty:
    # Clean Data
    try:
        df['total_amount'] = df['total_amount'].astype(str).str.replace(',', '', regex=True)
        df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
        df['delivery_datetime'] = pd.to_datetime(df['delivery_datetime'], errors='coerce')
    except: pass

    # Dashboard
    st.title("📊 Dashboard (Online)")
    total_sales = df['total_amount'].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Orders", f"{len(df)} ใบ")
    c2.metric("Total Sales", f"฿{total_sales:,.2f}")
    if 'status' in df.columns:
        c3.metric("Pending", len(df[df['status']=='Pending']), delta_color="inverse")
    
    st.divider()

    # --- ส่วนจัดการ Tabs (จุดที่แก้ Bug) ---
    tab1, tab2, tab3 = st.tabs(["📝 Pending", "🚚 Shipped", "❌ Canceled"])
    
    def render_tab(status_filter, tab_key):
        if 'status' not in df.columns: return
        
        filtered_df = df[df['status'] == status_filter]
        
        if filtered_df.empty:
            st.info("ไม่มีรายการ")
            return

        # 1. แสดงตาราง Data Editor
        st.write("##### ✅ เลือกรายการ:")
        df_show = filtered_df.copy()
        # สร้าง Column สำรองถ้าไม่มี
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
        
        # 2. ปุ่มจัดการ (ต้องอยู่นอก Loop 100%)
        col_act = st.columns([2, 1])
        with col_act[0]:
            # จุดสำคัญ: selectbox นี้จะถูกสร้างแค่ครั้งเดียวต่อ 1 Tab
            new_st = st.selectbox("เปลี่ยนสถานะ:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
        with col_act[1]:
            # ปุ่มกด
            if st.button(f"บันทึก ({tab_key})", key=f"btn_{tab_key}", type="primary"):
                ids = edited_df[edited_df.Select]['order_id'].tolist()
                if ids:
                    update_status(ids, new_st)
                    st.toast("บันทึกสำเร็จ!")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.toast("กรุณาเลือกรายการ")

        # 3. แสดงการ์ดรายละเอียด (Loop อยู่ตรงนี้)
        st.divider()
        st.caption("คลิกเพื่อดูบิล")
        for idx, row in filtered_df.iterrows():
            with st.expander(f"{row['order_id']} | {row['customer']} | ฿{row['total_amount']:,.2f}"):
                if 'raw_html' in row and row['raw_html']:
                    components.html(row['raw_html'], height=600, scrolling=True)
                else:
                    st.warning("ไม่พบรูปบิล")

    # เรียกใช้งาน
    with tab1: render_tab("Pending", "t1")
    with tab2: render_tab("Shipped", "t2")
    with tab3: render_tab("Canceled", "t3")

else:
    st.warning("⚠️ ไม่พบข้อมูล หรือยังไม่ได้เชื่อมต่อ Google Sheets")