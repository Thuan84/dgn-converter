# Hướng dẫn Deploy DGN Converter lên Render.com

## Bước 1: Push code lên GitHub

Thư mục `dgn-converter/` cần được push lên một repository GitHub riêng (hoặc chung với project, nhưng riêng sẽ dễ quản lý hơn).

### Tạo repo riêng cho converter:
```bash
cd dgn-converter
git init
git add .
git commit -m "DGN to KML converter API"
git remote add origin https://github.com/YOUR_USERNAME/dgn-converter.git
git push -u origin main
```

## Bước 2: Tạo Web Service trên Render.com

1. Đăng nhập https://dashboard.render.com
2. Click **"New +"** → chọn **"Web Service"**
3. Chọn **"Build and deploy from a Git repository"** → Connect repo `dgn-converter`
4. Cấu hình:
   - **Name**: `dgn-converter`
   - **Region**: Singapore (gần Việt Nam nhất)
   - **Runtime**: **Docker** (QUAN TRỌNG - phải chọn Docker, không phải Python)
   - **Instance Type**: **Free**
   - **Branch**: `main`
5. Click **"Create Web Service"**

## Bước 3: Đợi Build

- Render sẽ tự động build Docker image (lần đầu mất khoảng 5-10 phút)
- Khi xong, bạn sẽ thấy URL dạng: `https://dgn-converter.onrender.com`
- Test: Mở `https://dgn-converter.onrender.com/` → phải thấy `{"status":"ok","service":"dgn-converter"}`

## Bước 4: Cập nhật Frontend

Sau khi có URL từ Render, cập nhật biến môi trường trong frontend:

### Cách 1: Tạo file `.env` (khuyên dùng)
Tạo file `.env` tại root project `QuanLyCongTrinh`:
```
VITE_DGN_CONVERTER_URL=https://dgn-converter.onrender.com
```

### Cách 2: Sửa trực tiếp trong code
Mở file `components/BanDoKMLModal.tsx`, tìm dòng:
```typescript
const DGN_CONVERTER_URL = import.meta.env.VITE_DGN_CONVERTER_URL || 'https://dgn-converter.onrender.com';
```
Thay `https://dgn-converter.onrender.com` bằng URL thực tế của bạn.

## Lưu ý & Tối ưu Tốc độ

- **Bộ nhớ đệm IndexedDB (Tức thì 0.1s)**: Ứng dụng đã tích hợp tự động lưu bản đồ đã convert vào IndexedDB trên trình duyệt/thiết bị. Cùng 1 file CAD mở lại sẽ hiển thị tức thì mà không cần gọi server.
- **Pre-warming thông minh**: Ứng dụng tự động gửi tín hiệu ping đánh thức server ngay khi bạn mở bản đồ hoặc chọn file CAD, giúp giảm thời gian chờ đợi.
- **Giữ ấm 24/7 bằng Cron miễn phí (Khuyên dùng)**:
  - Để server Render Free không bao giờ ngủ đông:
    1. Đăng ký tài khoản miễn phí tại [cron-job.org](https://cron-job.org) hoặc [uptimerobot.com](https://uptimerobot.com).
    2. Tạo một Job mới ping URL: `https://dgn-converter.onrender.com/health` mỗi 10 phút.
    3. Hoặc sử dụng GitHub Actions workflow có sẵn tại `.github/workflows/keepalive-dgn.yml`.
- **File size**: Giới hạn 15MB cho mỗi file DGN upload.
- **Hệ tọa độ**: Nếu file DGN dùng VN2000, nhớ chọn đúng hệ tọa độ (Múi 3 hoặc Múi 6) khi upload để bản đồ hiển thị đúng vị trí.
