# Thành phần bên thứ ba

Tài liệu này liệt kê các thành phần không do bản này viết, cùng giấy phép của chúng. Tên giấy phép giữ nguyên tiếng Anh. Mục ghi **Chưa xác minh** là thông tin không có trong bản sao này. Cần kiểm tra trên trang của dự án trước khi phát hành.

## OwO-Dusk

- Nguồn: OwO-Dusk của EchoQuill. Bản này được chỉnh sửa từ bản đó.
- Giấy phép: GNU General Public License v3.0 (GPL-3.0). Xem [LICENSE](LICENSE).
- Dòng bản quyền: Copyright (c) 2024-present EchoQuill

## Thư viện được chép vào bản sao

### pytesseract

- Vị trí: `third_party/python/pytesseract/`. Các file được chép nguyên vẹn, không sửa.
- Phiên bản: 0.3.13. Nguồn: https://pypi.org/project/pytesseract/0.3.13/
- Giấy phép: Apache License 2.0. Bản đầy đủ nằm trong `third_party/python/pytesseract/LICENSE`.

## Phần mềm không đi kèm

### Tesseract OCR

- Không có trong bản sao. `setup.py` có thể tải bộ cài Tesseract cho Windows từ trang phát hành của UB-Mannheim trên GitHub, khi bạn đồng ý. Trên macOS, Linux và Termux, setup gợi ý cài bằng `brew`, `apt` hoặc `pkg`.
- Giấy phép: Chưa xác minh.

## Thư viện trong requirements.txt

Các gói này được cài khi chạy setup, và mã nguồn của chúng không có trong bản sao. Giấy phép của từng gói nằm trên trang PyPI của gói, hoặc trên repo với gói cài từ GitHub. Bản sao này không kiểm tra được các giấy phép đó.

| Gói | Ràng buộc | Giấy phép |
|---|---|---|
| discord.py-self | cài từ GitHub, commit `20ae80b398ec83fa272f0a96812140e14868c88f` | Chưa xác minh (xem repo của gói) |
| Flask | `==3.1.3` | xem trang PyPI |
| werkzeug | `>=3.1` | xem trang PyPI |
| aiosqlite | `==0.22.1` | xem trang PyPI |
| rich | `==15.0.0` | xem trang PyPI |
| requests | `==2.34.2` | xem trang PyPI |
| aiohttp | `==3.14.3` | xem trang PyPI |
| pytz | `==2026.4` | xem trang PyPI |
| numpy | không ghim | xem trang PyPI |
| pillow | không ghim | xem trang PyPI |
| onnxruntime | không ghim | xem trang PyPI |
| packaging | không ghim | xem trang PyPI |
| playsound3 | không ghim, chỉ dùng trên máy để bàn | xem trang PyPI |
| plyer | không ghim, chỉ dùng trên máy để bàn | xem trang PyPI |
| psutil | không ghim, tuỳ chọn | xem trang PyPI |

## Tài nguyên đi kèm

| Tệp | Mô tả | Nguồn và giấy phép |
|---|---|---|
| `website/static/imgs/logo.png` | Logo của dự án | Chưa xác minh |
| `audio/Beep.mp3` | Âm thanh khi có captcha | Chưa xác minh |
| `utils/data/emojis.json` | Dữ liệu emoji | Chưa xác minh |
| `utils/captcha_solver/best.onnx` | Mô hình ONNX của bộ giải captcha ảnh | Chưa xác minh |
| `config/webhookContent.json` | Mẫu thông báo webhook, có ảnh đại diện trỏ tới imgur.com | Chưa xác minh |

## Dịch vụ và thư viện bên ngoài được thêm trong bản cập nhật

Các mục dưới đây được bản cập nhật này gọi hoặc tải khi chạy. Giấy phép và điều khoản của từng mục chưa được kiểm tra, nên ghi là chưa xác minh.

- `https://blv-anh-ngok-said.onrender.com/api/quotes/{id}`: dịch vụ câu nói tiếng Việt, dùng cho lệnh tăng cấp khi `commands.lvlGrind.quoteSource` là `api` và lệnh tăng cấp bật. Giấy phép và điều khoản: Chưa xác minh.
- `https://api.ocr.space/parse/image`: dịch vụ OCR dự phòng cho ảnh nhiệm vụ, chỉ khi đã đặt `ocrApi`. Giấy phép và điều khoản: Chưa xác minh.
- Chart.js 4.5.1, hammer.js (URL không ghi phiên bản), chartjs-plugin-zoom 2.2.0: tải từ CDN cho trang cũ `/legacy` (`website/templates/index.html`). Giấy phép: Chưa xác minh.
- `website/static/v2/icons.svg`: biểu tượng SVG của dashboard. Nguồn và giấy phép: Chưa xác minh.
- `website/static/v2/liquid.css`, `liquid.js`, `glass-wallpaper.js`: giao diện phẳng và nền động WebGL của dashboard. Không dùng thư viện, phông chữ hay tài nguyên tải từ bên ngoài. Nguồn gốc mã: Chưa xác minh.

## Ghi chú tham chiếu trong mã nguồn

Các chú thích sau ghi nguồn tham khảo. Giấy phép của mã được tham khảo chưa được kiểm tra.

- `core/cogs/gems.py`: tham chiếu tới Discord-OwO-Bot của ChristopherBThai. Giấy phép: Chưa xác minh.
- `utils/huntbot/huntBotSolver.py`: có mã gốc của realphandat, lấy từ `phandat-selfbot`. Giấy phép: Chưa xác minh.
- `utils/misspell.py`: tham khảo `TypoCreator.py` trong `GeorgVetterGit/typo_generator`. Giấy phép: Chưa xác minh.

## Ghi chú

- Ảnh chụp màn hình và tài liệu hướng dẫn của bản gốc không còn trong bản sao này.
