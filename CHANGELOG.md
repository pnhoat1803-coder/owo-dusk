# Changelog

## Bản cập nhật sau 2.6.11 (chưa đóng gói)

### Thêm mới

- Dashboard v2: thanh điều hướng đặt được ở trái, phải, trên hoặc dưới và có thể ghim; trang tổng quan gọn; danh sách tất cả tài khoản; nút Start, Stop và Restart cho bot.
- Giao diện sáng, tối hoặc theo hệ thống; giao diện tiếng Việt và tiếng Anh; trang điện thoại `/m`.
- `dashboard.bat` và `dashboard.sh` mở dashboard riêng, không chạy bot.
- Kiểm tra cấp độ lưu lịch sử qua các lần chạy trong `utils/data/levels.sqlite`.
- Tự làm nhiệm vụ (quest): giới hạn số lệnh gửi, lệnh dừng an toàn, đếm nhiệm vụ từ kết quả của chính tài khoản, trạng thái lưu trong `utils/data/quests.sqlite`, OCR bằng Tesseract trước khi dùng ocr.space. Tắt mặc định, chỉ chạy cho tài khoản trong `auto_quest_accounts`.
- Gateway gọn (`gateway.lean`), bật mặc định.
- Câu nói tiếng Việt cho lệnh tăng cấp lấy từ `blv-anh-ngok-said.onrender.com` (`commands.lvlGrind.quoteSource`), nạp trước khi tài khoản đăng nhập và làm mới mỗi 6 giờ; lỗi mạng thì dùng `config/level_lines.txt`. Bot chỉ gọi dịch vụ khi lệnh tăng cấp và `useQuoteInstead` bật.
- Giao diện dashboard phẳng, tối giản, nền động WebGL nhẹ tự dừng khi ẩn tab hoặc khi hệ điều hành bật giảm chuyển động; các hiệu ứng chuyển trang, thẻ và đổi giao diện sáng tối.
- Khoá `website.host` chọn địa chỉ nghe của dashboard (mặc định `0.0.0.0`); khi khởi động bot in địa chỉ LAN, hoặc lý do dashboard chỉ mở trên máy này.
- Vết xử lý captcha (`debug.captchaTrace`), tắt mặc định; mức nhật ký console và file (`debug.consoleLevel`, `debug.fileLevel`).
- Theo dõi tin nhắn DM của OwO để xác nhận captcha đã được giải. Tạm dừng 30 giây sau mỗi lần sac thành công và chờ 900 giây giữa hai lần sac. Xếp lại hunt và battle khi OwO không trả lời.

### Sửa lỗi

- Khi lệch phiên bản cơ sở dữ liệu được nâng cấp tại chỗ, không bị xoá.
- Kiểm tra từ xa chỉ đặt cờ tạm dừng riêng, không đặt cờ captcha, và tự gỡ khi kiểm tra cho phép.
- `tokens.txt` được đọc qua một hàm chung: dòng trống và dòng bắt đầu bằng `#` bị bỏ qua; dòng sai định dạng được báo theo số dòng, không in nội dung.
- Các file khởi động chuyển về thư mục dự án trước khi import module của dự án.
- Thông báo khi webhook captcha không hợp lệ không in URL của webhook.
- Giá trị thử nghiệm của lần kiểm tra cấp độ đầu tiên (2 và 10 phút) được đưa về 15 và 75 phút.
- Nền động của dashboard không để canvas trống khi bật giảm chuyển động (vẽ một khung tĩnh rồi dừng) và không chạy vòng vẽ rỗng khi tạm dừng.
- Khi chưa đặt `ocrApi`, bot không gọi ocr.space và không dùng khoá thử nghiệm công khai.

### Thay đổi

- Mật khẩu để mở dashboard từ mạng LAN cần ít nhất 8 ký tự (trước là 6). Mật khẩu trống hoặc ngắn thì dashboard chỉ mở trên máy này.
- `website.enableHost` không còn dùng để chọn địa chỉ nghe, chỉ `website.host` quyết định.
- Bỏ ô chọn hiệu ứng (Kính mờ, Đặc) trên dashboard: chỉ còn một giao diện phẳng.
- Khi lưu cấu hình trên dashboard không còn ghi file `.bak`.
- Khi khởi động không còn hỏi đánh giá repo.
- `utils/data/weekly_runtime.json` không còn trong bản phát hành; bot tự tạo khi chạy.
- Các khoá lệnh chạy trên máy và đường dẫn Tesseract chỉ sửa được trong `config/global_settings.json`.

### Bảo mật

- Các khoá lệnh chạy trên máy (`console.commandToRunOnCaptcha`, `console.commandToRunOnBan`, `console.runConsoleCommandOnCaptcha`, `console.runConsoleCommandOnBan`, `levelCheck.tesseractPath`) không đổi được từ dashboard. Yêu cầu đổi trả lỗi `locked_key`.
- Mọi yêu cầu POST tới `/api/v2/` cần mã CSRF của phiên đăng nhập.
- Đăng nhập sai nhiều lần sẽ bị khoá tạm thời.
- Nhật ký che token Discord, URL webhook và khoá API.
- Dashboard không bao giờ mở cho mạng LAN khi chưa có mật khẩu đủ dài, kể cả khi `website.host` là `0.0.0.0`.

## 2.6.11 (bản âu-âu)

Bản này dựa trên OwO-Dusk 2.6.11 của EchoQuill. Các mục dưới đây là thay đổi của bản này.

### Thêm mới

- Bộ cài đặt và khởi động: `setup.bat` và `setup.sh` kiểm tra Python, pip, thư viện, Tesseract (trên Windows, tải bản portable khi bạn đồng ý), thư mục chạy, file cấu hình và OCR. `start.bat`, `start.sh` và `dashboard.bat` khởi động bot hoặc dashboard.
- Tự dừng theo thời gian: khoá `autoStop` (bật mặc định, 133 phút) và tham số `--max-runtime` từ 1 đến 10080 phút.
- Thông báo rõ khi `tokens.txt` không có tài khoản nào hoặc có dòng sai định dạng. Thông báo không in nội dung dòng.
- Thông báo khi dashboard chưa đặt mật khẩu hoặc mật khẩu quá ngắn.
- Kiểm tra cấp độ bằng OCR (`levelCheck`) với Tesseract, và nhật ký `utils/data/level_debug.jsonl` khi `levelCheck.debug` bật.
- Nhật ký chạy dạng văn bản trong `logs/`, có giới hạn dung lượng và số file giữ lại (`debug.logInTextFile`, `debug.logMaxMB`, `debug.logKeepFiles`).
- Dashboard lưu được cấu hình sau khi kiểm tra kiểu dữ liệu và giới hạn, và gửi được lệnh điều khiển tài khoản qua hàng đợi.
- Cấu hình nhà cung cấp giải captcha bên thứ ba trong `config/captcha.toml`. Mặc định tắt.
- Tài liệu tiếng Việt: `README.md`, `docs/cau-hinh.md`, `THIRD_PARTY_NOTICES.md` và file này.

### Sửa lỗi

- Dashboard không sửa được các khoá có thể chạy chương trình trên máy: `console.commandToRunOnCaptcha`, `console.commandToRunOnBan`, `console.runConsoleCommandOnCaptcha`, `console.runConsoleCommandOnBan` và `levelCheck.tesseractPath`. Yêu cầu sửa các khoá này trả lỗi `locked_key`.
- Cơ sở dữ liệu được nâng cấp tại chỗ và giữ dữ liệu, không xoá rồi tạo lại.
- Khi kiểm tra an toàn từ xa báo an toàn trở lại, bot xoá cờ tạm dừng và tiếp tục gửi lệnh. Dòng log chỉ in khi bắt đầu và khi kết thúc tạm dừng.
- Chạy được từ thư mục khác: bot và setup đổi về thư mục dự án trước khi đọc đường dẫn tương đối.
- Khi không có mạng, kiểm tra phiên bản được bỏ qua và không làm dừng khởi động.
- Lỗi mạng tạm thời khi kiểm tra một nhà cung cấp captcha không làm dừng toàn bộ khởi động. Nhà cung cấp đó bị bỏ qua và có cảnh báo.
- Bỏ thuộc tính `captcha_handler` khỏi lớp client, vì thư viện Discord dùng đúng tên đó. Việc đặt lại tên làm mọi tài khoản lỗi khi khởi tạo.

### Thay đổi

- Không còn file `.bak`: lưu cấu hình ghi qua file tạm rồi thay thế. Chuyển đổi `utils/data/weekly_runtime.json` cũng không sao lưu file cũ.
- Đã bỏ `install.bat` và `install.sh` (cài bằng lệnh tải từ mạng). Dùng `setup.bat` hoặc `setup.sh`.
- Đã bỏ lời nhắc đánh giá kho mã khi khởi động, các liên kết hỗ trợ trên dashboard, phần quyên góp, ảnh chụp màn hình và liên kết cộng đồng của bản gốc khỏi tài liệu.
- Đã bỏ `CONTRIBUTING.md`, `core/components/readme.md`, `config/instructions` và `audio/instructions`. Nội dung hữu ích đã có trong `docs/cau-hinh.md`.
- Đã bỏ khoá `hideStarRepoMessage` trong `config/misc.json` vì không còn được dùng.
- Mặc định đổi: `webhook.enabled`, `image_solver.enabled`, `hcaptcha_solver.captchaly.enabled` và `hcaptcha_solver.yescaptcha.enabled` đều là `false`.
- `requirements.txt` thêm `werkzeug>=3.1`.
- Đã xoá `utils/errors/catbox.py`, mô-đun tải file lên dịch vụ bên ngoài. Không có chỗ nào trong bản này gọi nó.

### Bảo mật

- Các khoá có thể chạy chương trình trên máy bị khoá với dashboard (xem Sửa lỗi).
- Nhật ký chạy che token Discord, URL webhook và khoá API.
- Dashboard không hiện mật khẩu, khoá OCR và URL webhook.
- Khi chưa đặt mật khẩu, dashboard chỉ cho máy này truy cập. Khi mở ra mạng mà chưa có mật khẩu, bot in cảnh báo. Đăng nhập sai nhiều lần bị từ chối tạm thời.
- Webhook và các nhà cung cấp captcha bên thứ ba tắt mặc định.
- Thông báo lỗi `tokens.txt` không in nội dung dòng, để token không lộ ra console hoặc nhật ký.
- Đã bỏ khỏi bản sao ba ảnh thử nghiệm của thẻ cấp độ (`tests/fixtures/level_cards/`), vì chúng có tên hiển thị và ảnh đại diện thật, và không có test nào dùng.
- Đã để trống danh sách ID kênh mẫu trong `channelSwitcher.allUsers.channels`.
