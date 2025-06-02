from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import numpy as np
from mysql.connector import Error
from io import BytesIO
import threading
import cv2
import time
import os
import re
from werkzeug.utils import secure_filename
import decimal



# Định nghĩa múi giờ UTC+7 (Hồ Chí Minh)
UTC_PLUS_7 = timezone(timedelta(hours=7))


UPLOAD_FOLDER = 'image_data/vehicle_images'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


from db import init_db_connection, get_db_connection

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

latest_frame = None

@app.route('/upload', methods=['POST'])
def upload_frame():
    global latest_frame
    if 'frame' not in request.files:
        return 'No frame received', 400

    file = request.files['frame']
    img_bytes = np.frombuffer(file.read(), np.uint8)
    frame = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
    latest_frame = frame
    
    return 'Frame received', 200

@app.route('/video_feed', methods=['GET'])
def video_feed():
    def generate():
        global latest_frame
        while True:
            if latest_frame is not None:
                _, buffer = cv2.imencode('.jpg', latest_frame)
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                time.sleep(0.05)
    return app.response_class(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


def verify_vehicle(license_plate):
    print(f"Verifying vehicle with license plate: {license_plate}")
    
    connection = get_db_connection()
    if connection is None or not connection.is_connected():
        return False, "Database connection error", None

    try:
        cursor = connection.cursor(dictionary=True)
        query = """
            SELECT v.*, u.name as vehicle_owner 
            FROM vehicles v 
            LEFT JOIN users u ON v.user_id = u.id 
            WHERE v.license_plate = %s
        """
        cursor.execute(query, (license_plate,))
        vehicle = cursor.fetchone()
        cursor.close()

        if vehicle:
            return True, "Xe đã đăng ký", vehicle
        return False, "Xe chưa đăng ký", None

    except Error as e:
        return False, f"Database error: {e}", None


@app.route('/entrance_LPR', methods=['POST'])
def send_data_entrance():
    data = request.json
    license_plate = data.get('license_plate', '')
    current_time = datetime.now(UTC_PLUS_7)  # Sử dụng múi giờ UTC+7

    # Đầu tiên, lấy thông tin xe từ biển số
    is_registered, status_message, vehicle = verify_vehicle(license_plate)
    
    # Nếu xe không đăng ký, trả về thông báo
    if not is_registered:
        vehicle_info = {        
            'license_plate': license_plate,
            'status': 'Xe chưa đăng ký',
            'is_registered': False,
            'entry_time': current_time.strftime("%H:%M %d/%m/%Y"),
            'type': 'entrance'
        }
        
        socketio.emit('vehicle_info', vehicle_info)
        
        return jsonify({
            'status': 'Unregistered vehicle detected',
            'data': vehicle_info
        }), 200

    # Nếu xe đã đăng ký, tiếp tục xử lý
    vehicle_id = vehicle['id']  # Lấy id của xe từ kết quả verify_vehicle
    
    connection = get_db_connection()
    if connection is None:
        return jsonify({'error': 'Database connection failed'}), 500

    try:
        cursor = connection.cursor(dictionary=True)
          # Kiểm tra lần quét gần nhất của xe này (bất kể vào hay ra)
        last_scan_query = """
            SELECT time_in, time_out FROM histories 
            WHERE vehicle_id = %s
            ORDER BY COALESCE(time_out, time_in) DESC LIMIT 1
        """
        cursor.execute(last_scan_query, (vehicle_id,))
        last_scan = cursor.fetchone()
        
        # Nếu có lần quét gần đây, kiểm tra thời gian
        if last_scan:
            # Lấy thời gian gần nhất (time_out nếu có, nếu không thì time_in)
            last_time = last_scan['time_out'] if last_scan['time_out'] else last_scan['time_in']
            
            # Nếu last_time là naive datetime (không có thông tin múi giờ), thêm múi giờ UTC+7
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=UTC_PLUS_7)
                
            time_diff = (current_time - last_time).total_seconds() / 60  # Phút
            
            # Nếu thời gian giữa hai lần quét < 1 phút, bỏ qua quét này
            if time_diff < 1:
                return jsonify({
                    'status': 'Ignored', 
                    'message': 'Same vehicle detected within 1 minute, ignoring...',
                    'license_plate': license_plate
                }), 200
        
        # Kiểm tra xem xe này đã có trong bãi đỗ chưa
        check_existing = """
            SELECT h.id, h.time_in, h.parking_space_id, p.space_number 
            FROM histories h
            LEFT JOIN parkingspace p ON h.parking_space_id = p.id
            WHERE h.vehicle_id = %s AND h.time_out IS NULL
            ORDER BY h.time_in DESC LIMIT 1
        """
        cursor.execute(check_existing, (vehicle_id,))
        existing_record = cursor.fetchone()
        
        # Nếu xe này đã có trong bãi đỗ (có bản ghi chưa có time_out), 
        # thì đây là xe đang ra
        if existing_record:
            # Cập nhật thời gian ra
            update_exit = """
                UPDATE histories 
                SET time_out = %s 
                WHERE id = %s
            """
            cursor.execute(update_exit, (current_time, existing_record['id']))
            
            # Cập nhật trạng thái chỗ đỗ xe dựa trên parking_space_id
            if existing_record['parking_space_id']:
                update_space = """
                    UPDATE parkingspace
                    SET is_occupied = 0 
                    WHERE id = %s
                """
                cursor.execute(update_space, (existing_record['parking_space_id'],))
                space_info = f"Chỗ đỗ {existing_record['space_number']} đã được giải phóng"
            else:
                # Fallback nếu không có parking_space_id
                update_space = """
                    UPDATE parkingspace
                    SET is_occupied = 0 
                    WHERE is_occupied = 1 
                    LIMIT 1
                """
                cursor.execute(update_space)
                space_info = "Một chỗ đỗ đã được giải phóng"
            
           
            
            # Tính phí đỗ xe
            time_in = existing_record['time_in']
            if time_in.tzinfo is None:
                time_in = time_in.replace(tzinfo=UTC_PLUS_7)
            
            duration = current_time - time_in
            total_minutes = duration.total_seconds() // 60
            fee = int(total_minutes * 167)  # Tính phí: 167 VNĐ/phút
            
            # Kiểm tra số dư tài khoản của user
            cursor.execute("SELECT balance FROM users WHERE id = %s", (vehicle['user_id'],))
            user = cursor.fetchone()
            balance = user['balance'] if user else decimal.Decimal('0.00')
            
            # Xác định trạng thái thanh toán
            payment_status = balance >= decimal.Decimal(str(fee))
            
            # Lưu vào bảng payments
            insert_payment = """
                INSERT INTO payments (history_id, fee, status, created_at)
                VALUES (%s, %s, %s, %s)
            """
            cursor.execute(insert_payment, (
                existing_record['id'],
                float(fee),  # Lưu dưới dạng float
                1 if payment_status else 0,  # status = 1 nếu đủ tiền, 0 nếu không đủ
                current_time
            ))
            payment_id = cursor.lastrowid
            
            # Nếu số dư đủ, trừ tiền và lưu giao dịch vào transaction_history
            if payment_status:
                update_balance = "UPDATE users SET balance = balance - %s WHERE id = %s"
                cursor.execute(update_balance, (float(fee), vehicle['user_id']))
                
                insert_transaction = """
                    INSERT INTO transaction_history (user_id, transaction_type, amount, payment_method, status, created_at, payment_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(insert_transaction, (
                    vehicle['user_id'],
                    'Phí đỗ xe',
                    -float(fee),
                    'Account Balance',
                    'COMPLETED',
                    current_time,
                    payment_id
                ))



            connection.commit()
            
            parking_duration = round((current_time - time_in).total_seconds() / 3600, 1)  # Giờ đỗ xe
            
            # Tạo dữ liệu phản hồi cho xe ra
            vehicle_exit = {
                'license_plate': license_plate,
                'status': status_message,
                'is_registered': is_registered,
                'exit_time': current_time.strftime("%H:%M %d/%m/%Y"),
                'type': 'exit',
                'parking_duration': parking_duration,
                'space_info': space_info,
                'fee': float(fee),  # Thêm phí đỗ xe
                'payment_status': 'Paid' if payment_status else 'Pending',
                'balance': float(balance)  # Số dư trước khi trừ
            }
            
            if vehicle:
                vehicle_exit.update({
                    'vehicle_type': vehicle['vehicle_type'],
                    'user_id': vehicle['user_id'],
                    'vehicle_owner': vehicle['vehicle_owner']
                })
            
            socketio.emit('vehicle_exit', vehicle_exit)
            
            return jsonify({
                'status': 'Vehicle exit processed successfully',
                'data': vehicle_exit
            }), 200
            
        else:
            # Đây là xe đang vào
            # Tìm một chỗ đỗ xe trống
            find_empty_space = """
                SELECT id, space_number, level
                FROM parkingspace 
                WHERE is_occupied = 0 
                LIMIT 1
            """
            cursor.execute(find_empty_space)
            empty_space = cursor.fetchone()
            
            if empty_space:
                # Cập nhật chỗ đỗ thành đã có xe
                update_space = """
                    UPDATE parkingspace 
                    SET is_occupied = 1 
                    WHERE id = %s
                """
                cursor.execute(update_space, (empty_space['id'],))
                
                # Thêm vào lịch sử với parking_space_id
                insert_history = """
                    INSERT INTO histories (vehicle_id, time_in, parking_space_id)
                    VALUES (%s, %s, %s)
                """
                cursor.execute(insert_history, (vehicle_id, current_time, empty_space['id']))
                
                space_info = f"Sẽ đỗ tại chỗ {empty_space['space_number']} - Tầng {empty_space['level']}"
            else:
                # Nếu không tìm thấy chỗ đỗ trống
                insert_history = """
                    INSERT INTO histories (vehicle_id, time_in)
                    VALUES (%s, %s)
                """
                cursor.execute(insert_history, (vehicle_id, current_time))
                space_info = "Không tìm thấy chỗ đỗ trống"
            
            connection.commit()
            
            # Tạo dữ liệu phản hồi cho xe vào
            vehicle_info = {        
                'license_plate': license_plate,
                'status': status_message,
                'is_registered': is_registered,
                'entry_time': current_time.strftime("%H:%M %d/%m/%Y"),
                'type': 'entrance',
                'space_info': space_info
            }

            vehicle_info.update({
                'vehicle_type': vehicle['vehicle_type'],
                'user_id': vehicle['user_id'],
                'vehicle_owner': vehicle['vehicle_owner']
            })

            socketio.emit('vehicle_info', vehicle_info)
            
            return jsonify({
                'status': 'Vehicle entry processed successfully',
                'data': vehicle_info
            }), 200

    except Error as e:
        connection.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()


@socketio.on('connect')
def handle_connect():
    print("✅ A client connected!")


@app.route('/deposit_money/', methods=['POST'])
def deposit_money():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        amount = data.get('amount')
        payment_method = data.get('payment_method')
        transaction_type = 'Nạp tiền'
        status = 'COMPLETED'

        if not user_id or not amount:
            return jsonify({'error': 'Thiếu user_id hoặc amount'}), 400

        connection = get_db_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500

        cursor = connection.cursor(dictionary=True)
        # Cập nhật số dư
        update_balance = "UPDATE users SET balance = balance + %s WHERE id = %s"
        cursor.execute(update_balance, (amount, user_id))

        # Lấy số dư mới
        cursor.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        new_balance = user['balance'] if user else 0

        # Thêm lịch sử giao dịch
        insert_transaction = """
            INSERT INTO transaction_history (user_id, transaction_type, amount, payment_method, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        now = datetime.now(UTC_PLUS_7)# Sử dụng múi giờ UTC+7
        cursor.execute(insert_transaction, (user_id, transaction_type, amount, payment_method, status, now))
        transaction_id = cursor.lastrowid

         # Chạy tác vụ trừ tiền sau 30 giây
        threading.Timer(5, process_pending_payment, args=(user_id, new_balance)).start()

        connection.commit()
        cursor.close()

        return jsonify({
            'message': 'Nạp tiền thành công',
            'transaction_id': transaction_id,
            'amount': str(amount),
            'payment_method': payment_method,
            'new_balance': str(new_balance),
        }), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500



def process_pending_payment(user_id, new_balance):
    """Hàm xử lý một bản ghi Payment chưa thanh toán sau 30 giây"""
    time.sleep(5)  # Đợi 10 giây
    connection = get_db_connection()
    if connection is None:
        print(f"Error: Cannot connect to database for user {user_id}")
        return

    cursor = connection.cursor(dictionary=True)
    try:
        # Kiểm tra số dư hiện tại
        cursor.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        current_balance = cursor.fetchone()['balance']

        # Tìm bản ghi Payment chưa thanh toán
        cursor.execute("""
            SELECT p.id, p.fee, h.time_in, h.time_out
            FROM payments p
            JOIN histories h ON p.history_id = h.id
            JOIN vehicles v ON h.vehicle_id = v.id
            WHERE v.user_id = %s AND p.status = FALSE
            ORDER BY p.created_at DESC LIMIT 1
        """, (user_id,))
        payment = cursor.fetchone()

        if payment and current_balance >= payment['fee']:
            fee = decimal.Decimal(payment['fee'])
            # Trừ tiền
            cursor.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (float(fee), user_id))
            cursor.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
            current_balance = cursor.fetchone()['balance']

            # Cập nhật Payment
            cursor.execute("UPDATE payments SET status = TRUE WHERE id = %s", (payment['id'],))

            # Ghi giao dịch vào transaction_history
            insert_transaction = """
                INSERT INTO transaction_history (user_id, transaction_type, amount, payment_method, status, created_at, payment_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            now = datetime.now(UTC_PLUS_7)# Sử dụng múi giờ UTC+7
            cursor.execute(insert_transaction, (
                user_id, 'Phí đỗ xe', -float(fee), 'Account Balance', 'COMPLETED', now, payment['id']
            ))
            transaction_id = cursor.lastrowid

            # Lấy lại số dư mới nhất
            cursor.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            new_balance = user['balance'] if user else 0

            # Emit thông báo về client qua SocketIO
            socketio.emit('parking_fee_paid', {
                'message': 'Thanh toán phí gửi xe thành công',
                'transaction_id': transaction_id,
                'amount': str(-float(fee)),
                'payment_method': 'system',
                'new_balance': str(new_balance),
                'payment_id': payment['id'],
                'created_at': now.isoformat()
            }, namespace='/')  # namespace có thể thay đổi tùy client


            # Gửi tín hiệu mở cổng (giả lập)
            # gate_controller.open_gate(license_plate)

        connection.commit()
    except Error as e:
        connection.rollback()
        print(f"Error processing payment for user {user_id}: {str(e)}")
    finally:
        cursor.close()
        connection.close()

if __name__ == '__main__':
    init_db_connection()  # Kết nối duy nhất tại đây
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
