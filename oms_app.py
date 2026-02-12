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

# --- 1. SET PAGE CONFIG (ต้องอยู่บรรทัดแรกสุด) ---
st.set_page_config(page_title="Cloud OMS", layout="wide", page_icon="☁️")

# --- CONFIGURATION ---
IMAP_SERVER = "imap.gmail.com"
SHEET_NAME = "MyOrderDB"  # ชื่อไฟล์ Google Sheet ต้องตรงเป๊ะ

# --- CONNECT TO GOOGLE SHEETS (NEW STABLE VERSION) ---
def get_db_connection():
    try:
        # ตรวจสอบว่ามี Secrets หรือไม่
        if "gcp_service_account" not in st.secrets:
            st.error("ไม่พบ Secrets 'gcp_service_account' โปรดตรวจสอบการตั้งค่า")
            return None

        # แปลง Secrets เป็น Dict
        creds_dict = dict(st.secrets["gcp_service_account"])

        # ใช้คำสั่งมาตรฐานของ gspread (ไม่ต้องพึ่ง oauth2client แล้ว)
        client = gspread.service_account_from_dict(creds_dict)
        
        # เปิด Google Sheet
        sheet = client.open(SHEET_NAME)
        return sheet
        
    except Exception as e:
        st.error(f"🔥 เชื่อมต่อ Google Sheets ไม่ได้: {e}")
        return None

# --- DATABASE FUNCTIONS ---
@st.cache_data(ttl=300) # Cache ข้อมูล 5 นาทีเพื่อให้เร็ว
def load_orders():
    sh = get_db_connection()
    if sh is None: return pd.DataFrame()
    
    try:
        worksheet = sh.worksheet("orders")
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        return df
    except gspread.exceptions.WorksheetNotFound:
        st.error("ไม่พบแท็บ 'orders' ใน Google Sheet")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"โหลดข้อมูลไม่ได้: {e}")
        return pd.DataFrame()

def save_new_order(order_data):
    sh = get_db_connection()
    if sh is None: return False
    
    try:
        ws = sh.worksheet("orders")
        
        # เช็คซ้ำ (Duplicate Check)
        try:
            cell = ws.find(str(order_data['order_id']))
            if cell: return False 
        except:
            pass # หาไม่เจอ = ยังไม่มี (ดีแล้ว)

        row = [
            str(order_data['order_id']),
            order_data['customer'],
            str(order_data['order_date']),
            str(order_data['delivery_datetime']),
            order_data['total_amount'], # ส่งไปเป็นตัวเลขปกติ
            "Pending",
            order_data['raw_html']
        ]
        ws.append_row(row)
        return True
    except Exception as e:
        st.error(f"บันทึกข้อมูลล้มเหลว: {e}")
        return False

def update_status(order_ids, new_status):
    sh = get_db_connection()
    if sh is None: return

    try:
        ws = sh.worksheet("orders")
        log_ws = sh.worksheet("logs")
        
        if not isinstance(order_ids, list): order_ids = [order_ids]
        
        # หาตำแหน่ง Column 'status'
        header = ws.row_values(1)
        try:
            status_col_idx = header.index('status') + 1
        except ValueError:
            st.error("ไม่พบคอลัมน์ชื่อ 'status' ใน Google Sheet")
            return

        for oid in order_ids:
            try:
                cell = ws.find(str(oid))
                if cell:
                    ws.update_cell(cell.row, status_col_idx, new_status)
                    # บันทึก Log
                    log_ws.append_row([str(datetime.datetime.now()), str(oid), f"Updated to {new_status}"])
            except Exception as e:
                print(f"Error updating {oid}: {e}")
                
    except Exception as e:
        st.error(f"Update Error: {e}")

# --- EMAIL FETCHING ---
def sync_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        
        # ดึง Email/Pass จาก Secrets
        if "email" not in st.secrets:
            st.error("ไม่พบ Secrets 'email'")
            return 0
            
        email_user = st.secrets["email"]["user"]
        email_pass = st.secrets["email"]["password"]
        
        mail.login(email_user, email_pass)
        mail.select("inbox")
        
        # ค้นหาอีเมล
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
                    
                    # Logic การแกะข้อมูล (Regex)
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
                        # ลบลูกน้ำออกให้ชัวร์ก่อนแปลง
                        clean_num = re.sub(r'[^\d.]', '', total_match.group(1))
                        total = float(clean_num)
                    
                    # วันที่จัดส่ง (ตัวอย่าง +1 วัน)
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
            except Exception as e:
                print(f"Skipping email {num}: {e}")
                continue
        
        mail.logout()
        return new_count
        
    except Exception as e:
        st.error(f"Sync Failed: {e}")
        return 0

# --- MAIN UI DASHBOARD ---
st.sidebar.title("☁️ Cloud Order System")

if st.sidebar.button("🔄 Sync & Refresh", type="primary"):
    with st.spinner("กำลังดึงข้อมูลจาก Email..."):
        cnt = sync_emails()
        if cnt > 0: 
            st.sidebar.success(f"✅ เพิ่ม {cnt} ออเดอร์ใหม่")
            st.cache_data.clear() # ล้าง Cache เพื่อให้เห็นข้อมูลใหม่ทันที
        else: 
            st.sidebar.info("ยังไม่มีออเดอร์ใหม่")
        st.rerun()
# --- วางโค้ดนี้เพิ่มในส่วน Sidebar ---
st.sidebar.markdown("---")
st.sidebar.subheader("🔧 เมนูแก้ปัญหา")

if st.sidebar.button("test เชื่อมต่อ Google Sheet"):
    try:
        # 1. ลองเชื่อมต่อ
        if "gcp_service_account" not in st.secrets:
            st.sidebar.error("❌ ไม่พบการตั้งค่า Secrets")
        else:
            creds_dict = dict(st.secrets["gcp_service_account"])
            client = gspread.service_account_from_dict(creds_dict)
            
            # 2. ลองเปิดไฟล์
            st.sidebar.info(f"กำลังหาไฟล์ชื่อ: {SHEET_NAME} ...")
            sh = client.open(SHEET_NAME)
            st.sidebar.success(f"✅ เจอไฟล์แล้ว! ชื่อ: {sh.title}")
            
            # 3. ลองหาแท็บ orders
            ws = sh.worksheet("orders")
            st.sidebar.success(f"✅ เจอแท็บ 'orders'! มีข้อมูล {len(ws.get_all_records())} แถว")
            
    except gspread.exceptions.SpreadsheetNotFound:
        st.sidebar.error("❌ หาไฟล์ Google Sheet ไม่เจอ! (เช็คชื่อไฟล์ / การแชร์)")
    except gspread.exceptions.WorksheetNotFound:
        st.sidebar.error("❌ เจอไฟล์ แต่ไม่เจอแท็บชื่อ 'orders'")
    except Exception as e:
        st.sidebar.error(f"❌ Error อื่นๆ: {e}")
# โหลดข้อมูล
df = load_orders()

if not df.empty:
    # Cleanup Data (สำคัญมาก)
    try:
        # แปลงยอดเงินเป็นตัวเลข (ลบลูกน้ำออก)
        df['total_amount'] = df['total_amount'].astype(str).str.replace(',', '', regex=True)
        df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
        # แปลงวันที่
        df['delivery_datetime'] = pd.to_datetime(df['delivery_datetime'], errors='coerce')
    except Exception as e:
        st.error(f"Data Formatting Error: {e}")

    # --- Dashboard KPI ---
    st.title("📊 Dashboard (Online)")
    
    # คำนวณยอด
    total_sales = df['total_amount'].sum()
    total_orders = len(df)
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Orders", f"{total_orders} ใบ")
    col2.metric("Total Sales", f"฿{total_sales:,.2f}")
    
    if 'status' in df.columns:
        pending_count = len(df[df['status']=='Pending'])
        col3.metric("Pending Orders", f"{pending_count} ใบ", delta_color="inverse")
    
    st.divider()

    # --- Order Management Tabs ---
    tab1, tab2, tab3 = st.tabs(["📝 Pending", "🚚 Shipped", "❌ Canceled"])
    
    # ... (ส่วนอื่นเหมือนเดิม แก้เฉพาะฟังก์ชันนี้) ...

    def render_tab(status_filter, tab_key):
        # 1. ป้องกัน Error กรณีไม่มีคอลัมน์ status
        if 'status' not in df.columns: 
            return
        
        # 2. กรองข้อมูล
        filtered_df = df[df['status'] == status_filter]
        
        if filtered_df.empty:
            st.info("ไม่มีรายการในหมวดหมู่นี้")
            return

        # --- ส่วนตารางติ๊กเลือก (Bulk Action) ---
        st.write("##### ✅ เลือกรายการที่ต้องการเปลี่ยนสถานะ:")
        
        # เตรียมข้อมูล
        df_show = filtered_df.copy()
        if 'total_amount' not in df_show.columns: df_show['total_amount'] = 0.0
        if 'delivery_datetime' not in df_show.columns: df_show['delivery_datetime'] = ""

        df_display = df_show[['order_id', 'customer', 'total_amount', 'delivery_datetime']].reset_index(drop=True)
        df_display.insert(0, "Select", False)
        
        # แสดงตาราง Editor
        edited_df = st.data_editor(
            df_display,
            column_config={
                "Select": st.column_config.CheckboxColumn("เลือก", default=False, width="small"),
                "order_id": st.column_config.TextColumn("เลขที่ออเดอร์", width="medium"),
                "customer": st.column_config.TextColumn("ลูกค้า", width="large"),
                "total_amount": st.column_config.NumberColumn("ยอดเงิน", format="฿ %.2f"),
                "delivery_datetime": st.column_config.DatetimeColumn("เวลาส่ง", format="D MMM HH:mm")
            },
            key=f"editor_{tab_key}",
            use_container_width=True,
            hide_index=True
        )
            
        # --- ส่วนปุ่มกด (อยู่นอก Loop) ---
        # สังเกตว่าบรรทัดนี้ต้องชิดซ้าย เท่ากับ edited_df ด้านบน (ห้ามย่อหน้าเข้าไปลึก)
        col_actions = st.columns([2, 1])
        
        with col_actions[0]:
            # ตรงนี้คือจุดที่ Error ก่อนหน้านี้ (Key ซ้ำ)
            # ตอนนี้ปลอดภัยแล้ว เพราะมันอยู่นอก for loop
            new_st = st.selectbox("เปลี่ยนสถานะเป็น:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
            
        with col_actions[1]:
            selected_ids = edited_df[edited_df.Select]['order_id'].tolist()
            if st.button(f"ยืนยัน ({len(selected_ids)})", key=f"btn_{tab_key}", type="primary"):
                if selected_ids:
                    update_status(selected_ids, new_st)
                    st.toast("✅ บันทึกสถานะเรียบร้อย!")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.toast("⚠️ กรุณาติ๊กเลือกรายการก่อน")

        # --- ส่วนแสดงรายละเอียดการ์ด (Loop อยู่ตรงนี้) ---
        st.divider()
        st.caption("📄 คลิกที่ลูกศรเพื่อดูบิลต้นฉบับ")
        
        for idx, row in filtered_df.iterrows():
            # เช็คว่ามี HTML บิลไหม
            has_bill = 'raw_html' in row and row['raw_html'] and len(str(row['raw_html'])) > 10
            icon = "✅" if has_bill else "⚠️"
            
            with st.expander(f"{icon} {row['order_id']} | {row['customer']} | ฿{row['total_amount']}"):
                if has_bill:
                    components.html(row['raw_html'], height=600, scrolling=True)
                else:
                    st.warning("ไม่พบรูปบิล")       
            # ปุ่มดำเนินการ (ย้ายมาไว้ใต้ตารางให้กดง่าย)
            col_actions = st.columns([2, 1])
            with col_actions[0]:
                new_st = st.selectbox("เปลี่ยนสถานะเป็น:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
            with col_actions[1]:
                # ดึง ID ที่ถูกติ๊กเลือก
                selected_ids = edited_df[edited_df.Select]['order_id'].tolist()
                if st.button(f"ยืนยัน ({len(selected_ids)})", key=f"btn_{tab_key}", type="primary"):
                    if selected_ids:
                        update_status(selected_ids, new_st)
                        st.toast("✅ บันทึกสถานะเรียบร้อย!")
                        st.cache_data.clear() # ล้าง Cache
                        st.rerun() # รีโหลดหน้า
                    else:
                        st.toast("⚠️ กรุณาติ๊กเลือกรายการก่อน")
      
        # --- ส่วนแสดงบิล (Cards) ---
        st.divider()
        st.caption("📄 คลิกที่ลูกศรเพื่อดูบิลต้นฉบับ")
        for idx, row in filtered_df.iterrows():
            # เช็คว่ามี HTML บิลไหม
            has_bill = 'raw_html' in row and row['raw_html'] and len(str(row['raw_html'])) > 10
            icon = "✅" if has_bill else "⚠️"
            
            with st.expander(f"{icon} {row['order_id']} | {row['customer']} | ฿{row['total_amount']}"):
                if has_bill:
                    components.html(row['raw_html'], height=600, scrolling=True)
                else:
                    st.warning("ไม่พบรูปบิล (อาจเป็นเพราะตอน Sync ครั้งแรกยังไม่ได้สร้างหัวตาราง 'raw_html')")
        
        # ปุ่มดำเนินการ
        c_btn1, c_btn2 = st.columns([3, 1])
        with c_btn2:
            # ดึง ID ที่ถูกติ๊กเลือก
            selected_ids = edited_df[edited_df.Select]['order_id'].tolist()
            
            new_st = st.selectbox("เปลี่ยนสถานะ:", ["Pending", "Shipped", "Canceled"], key=f"sel_{tab_key}")
            
            if st.button("บันทึกสถานะ", key=f"btn_{tab_key}"):
                if selected_ids:
                    update_status(selected_ids, new_st)
                    st.success(f"อัปเดต {len(selected_ids)} รายการเรียบร้อย!")
                    st.cache_data.clear() # ล้าง Cache
                    st.rerun() # รีโหลดหน้า
                else:
                    st.toast("กรุณาเลือกรายการก่อนครับ", icon="⚠️")

        # แสดงรายละเอียดการ์ด (Expander)
        st.write("---")
        st.caption("คลิกที่รายการเพื่อดูบิลต้นฉบับ")
        for idx, row in filtered_df.iterrows():
            with st.expander(f"📄 {row['order_id']} | {row['customer']} | ฿{row['total_amount']:,.2f}"):
                if 'raw_html' in row and row['raw_html']:
                    components.html(row['raw_html'], height=500, scrolling=True)
                else:
                    st.warning("ไม่พบรูปบิล")

    # เรียกใช้ฟังก์ชันแสดงผลแต่ละ Tab
    with tab1: render_tab("Pending", "t1")
    with tab2: render_tab("Shipped", "t2")
    with tab3: render_tab("Canceled", "t3")

else:
    st.warning("⚠️ ไม่พบข้อมูลในระบบ หรือเชื่อมต่อ Google Sheets ไม่ได้")
    st.info("ลองกดปุ่ม Sync ด้านซ้ายมือเพื่อดึงข้อมูลใหม่")