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

# --- 1. SET PAGE CONFIG ---
st.set_page_config(page_title="Cloud OMS Pro", layout="wide", page_icon="📦")

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
        
        # Clean Columns
        df.columns = df.columns.str.strip()
        
        # Handle Missing Columns (ถ้าลืมเพิ่มใน Sheet จะได้ไม่ Error)
        for col in ['requested_date', 'remarks', 'total_qty', 'raw_html']:
            if col not in df.columns:
                df[col] = ""

        # Fill NaN
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
            if cell: return False # Duplicate
        except:
            pass 
        
        # เรียงข้อมูลให้ตรงกับหัวตาราง
        row = [
            str(order_data['order_id']),
            order_data['customer'],
            str(order_data['order_date']),
            str(order_data['delivery_datetime']),
            order_data['total_amount'],
            "Pending",
            order_data['raw_html'],
            order_data['requested_date'], # New
            order_data['remarks'],        # New
            order_data['total_qty']       # New
        ]
        ws.append_row(row)
        return True
    except Exception as e:
        print(f"Save Error: {e}")
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

# --- EMAIL FETCHING & PARSING (UPGRADED) ---
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
        # ดึง 10 ฉบับล่าสุด
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
                    
                    # 1. Basic Info
                    order_match = re.search(r"(SO-\d+-\d+)", text)
                    if not order_match: continue
                    order_id = order_match.group(1)
                    
                    customer = "Unknown"
                    cust_match = re.search(r"received from\s(.*?)\swith order no", text)
                    if not cust_match: cust_match = re.search(r"(.*?)\shas placed a new order", text)
                    if cust_match: customer = cust_match.group(1).strip()
                    
                    total = 0.0
                    total_match = re.search(r"Total Amount\s*฿\s*([\d,]+\.?\d*)", text)
                    if total_match: 
                        total = float(re.sub(r'[^\d.]', '', total_match.group(1)))
                    
                    # 2. New Fields Extraction
                    # Requested Delivery Date
                    req_date = ""
                    req_match = re.search(r"Requested Delivery Date:\s*(.*)", text)
                    if req_match: req_date = req_match.group(1).strip()
                    
                    # Remarks
                    remarks = ""
                    rem_match = re.search(r"Remarks:\s*(.*)", text)
                    if rem_match: remarks = rem_match.group(1).strip()

                    # 3. Calculate Total Qty (Parse Table)
                    total_qty = 0
                    try:
                        tables = soup.find_all("table")
                        for table in tables:
                            rows = table.find_all("tr")
                            if not rows: continue
                            # หาหัวตาราง
                            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
                            if "qty" in headers:
                                qty_idx = headers.index("qty")
                                for row in rows[1:]:
                                    cols = row.find_all("td")
                                    if len(cols) > qty_idx:
                                        val = cols[qty_idx].get_text(strip=True)
                                        try:
                                            total_qty += float(re.sub(r'[^\d.]', '', val))
                                        except: pass
                                break # เจอแล้วหยุด
                    except: pass
                    
                    # Default Delivery Date
                    delivery_dt = datetime.datetime.now() + timedelta(days=1) 

                    order_data = {
                        'order_id': order_id,
                        'customer': customer,
                        'order_date': datetime.date.today(),
                        'delivery_datetime': delivery_dt,
                        'total_amount': total,
                        'raw_html': html_body,
                        'requested_date': req_date,
                        'remarks': remarks,
                        'total_qty': total_qty
                    }
                    
                    if save_new_order(order_data):
                        new_count += 1
            except: continue
        mail.logout()
        return new_count
    except Exception: return 0

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
    # Data Prep
    try:
        df['total_amount'] = df['total_amount'].astype(str).str.replace(',', '', regex=True)
        df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
        df['order_date'] = pd.to_datetime(df['order_date'], errors='coerce').dt.date
        if 'total_qty' in df.columns:
            df['total_qty'] = pd.to_numeric(df['total_qty'], errors='coerce').fillna(0)
    except: pass

    st.title("📊 Dashboard")
    
    # 1. Metrics Updated
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📦 Total Orders", f"{len(df)}")
    c2.metric("💰 Total Sales", f"฿{df['total_amount'].sum():,.0f}")
    c3.metric("🔢 Total Items (Qty)", f"{df['total_qty'].sum():,.0f} ชิ้น")
    if 'status' in df.columns:
        c4.metric("⏳ Pending", len(df[df['status']=='Pending']), delta_color="inverse")
    
    st.divider()

    # 2. Graphs
    g1, g2 = st.columns([2, 1])
    with g1:
        daily = df.groupby('order_date')['total_amount'].sum().reset_index()
        fig = px.bar(daily, x='order_date', y='total_amount', title="ยอดขายรายวัน", color_discrete_sequence=['#36A2EB'])
        st.plotly_chart(fig, use_container_width=True)
    with g2:
        if 'status' in df.columns:
            status_c = df['status'].value_counts().reset_index()
            status_c.columns = ['Status', 'Count']
            fig2 = px.pie(status_c, values='Count', names='Status', title="สัดส่วนสถานะ", hole=0.4)
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # --- Order Management ---
    tab1, tab2, tab3 = st.tabs(["📝 Pending", "🚚 Shipped", "❌ Canceled"])
    
    def render_tab(status_filter, tab_key):
        if 'status' not in df.columns: return
        filtered_df = df[df['status'] == status_filter]
        
        if filtered_df.empty:
            st.info("ไม่มีรายการ")
            return

        # TABLE UI
        st.write(f"##### ✅ รายการ ({len(filtered_df)})")
        df_show = filtered_df.copy()
        df_show.insert(0, "Select", False)
        
        # แสดงคอลัมน์ใหม่ในตาราง
        cols_config = {
            "Select": st.column_config.CheckboxColumn("เลือก", width="small"),
            "order_id": st.column_config.TextColumn("Order No."),
            "customer": st.column_config.TextColumn("Customer"),
            "requested_date": st.column_config.TextColumn("Req. Date"), # New
            "total_qty": st.column_config.NumberColumn("Qty", format="%d"), # New
            "total_amount": st.column_config.NumberColumn("Total", format="฿ %.2f")
        }
        
        # เลือกเฉพาะคอลัมน์ที่มีจริง
        display_cols = ['Select', 'order_id', 'customer', 'requested_date', 'total_qty', 'total_amount']
        display_cols = [c for c in display_cols if c in df_show.columns]

        edited_df = st.data_editor(
            df_show[display_cols],
            column_config=cols_config,
            key=f"editor_{tab_key}",
            use_container_width=True,
            hide_index=True
        )
        
        # BUTTONS
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

        # DETAILS CARDS
        st.write("---")
        for idx, row in filtered_df.iterrows():
            # Check bill HTML
            html_content = str(row.get('raw_html', ''))
            has_bill = len(html_content) > 50
            icon = "📄" if has_bill else "⚠️"
            
            # Label
            label = f"{icon} {row['order_id']} | {row['customer']} | Qty: {row.get('total_qty',0)} | ฿{row['total_amount']:,.2f}"
            
            with st.expander(label):
                c_info1, c_info2 = st.columns(2)
                with c_info1:
                    st.markdown(f"**Requested Date:** {row.get('requested_date', '-')}")
                    st.markdown(f"**Remarks:** {row.get('remarks', '-')}")
                
                if has_bill:
                    components.html(html_content, height=600, scrolling=True)
                else:
                    st.warning("ไม่พบรูปบิล (ลองกด Forward เมลเข้ามาใหม่แล้ว Sync)")

    with tab1: render_tab("Pending", "t1")
    with tab2: render_tab("Shipped", "t2")
    with tab3: render_tab("Canceled", "t3")

else:
    st.warning("⚠️ ไม่พบข้อมูล")
    st.info("อย่าลืมเพิ่มคอลัมน์ใน Google Sheets: requested_date, remarks, total_qty")