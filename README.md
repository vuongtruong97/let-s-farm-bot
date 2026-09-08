# Let's Farm Auto

Bot thu hoạch và trồng cho **Let's Farm** (Playday Games) khi game chạy trên **BlueStacks Windows**.

Let's Farm chơi bằng **kéo tay** trên ruộng. Bot chụp màn hình qua ADB, **tự tìm ô đất / cây chín / túi hạt giống**, rồi vuốt theo hàng. Không cần căn lưới trên app.

Trên máy không có BlueStacks, app chạy **mô phỏng nông trại** để xem bot nhận diện và auto.

## Chạy trên Windows (BlueStacks)

1. Cài [Python 3.11+](https://www.python.org/downloads/). Khi cài, tick **Add python.exe to PATH**.
2. Mở BlueStacks → **Settings → Advanced** → bật **Android Debug Bridge (ADB)**.
3. Mở Let's Farm, zoom sao cho **thấy cả vườn** và thanh hạt giống phía dưới.
4. Trong thư mục project:

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m letsfarm_auto --host 127.0.0.1 --port 48721
```

5. Mở [http://127.0.0.1:48721](http://127.0.0.1:48721)
6. Bấm **Tìm BlueStacks** rồi **Bắt đầu**.

BlueStacks 5 có `HD-Adb.exe` sẵn. Bot tự tìm file này và đọc cổng từ `bluestacks.conf` nếu cổng bị đổi.

## Bot tự nhận diện

Mỗi vòng quét, bot:

- Lọc HUD / thanh túi đồ
- Tìm các thửa đất (màu đất + nông sản chín)
- Gom thành hàng, vuốt các ô chín rồi chọn hạt giống và trồng ô trống
- Đánh dấu túi hạt giống ở thanh dưới (ô saturations bên trái)

Chế độ:

- **Thu hoạch** — chỉ vuốt ô chín
- **Trồng** — chạm hạt giống rồi vuốt ô trống
- **Thu hoạch + trồng** — thu hoạch, quét lại, rồi trồng

Phím tắt: **F8** chạy, **F9** dừng.

Nếu nhận sai: kéo camera Let's Farm để cả ruộng nằm trong khung, tránh popup che đất.

## Chạy mô phỏng (không cần game)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m letsfarm_auto --host 127.0.0.1 --port 48721
```

Mở `http://127.0.0.1:48721`, **Quét thử** / **Bắt đầu**. Bản mô phỏng chín sau vài giây.

## Kiểm tra

```bash
pip install -e ".[dev]"
pytest -q
```

## Lưu ý

- Macro phía client (chụp màn hình + `input swipe`). Không inject bộ nhớ, không sửa file game.
- Tự động hóa có thể trái điều khoản Let's Farm. Dùng trên tài khoản của bạn, nghỉ giữa vòng.
- Giữ cửa sổ BlueStacks hiện Let's Farm, không bị che.

Cấu hình lưu tại `data/config.json`.
