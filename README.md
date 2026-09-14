# Hay Day Bot

Bot tự chơi Hay Day trên emulator Android (ưu tiên BlueStacks) theo vòng lặp:

```text
SCREENSHOT → VISION → GAME STATE → DECISION → ACTION → VERIFY
```

Không hard-code chuỗi click. Không tự tiêu diamond (`allow_diamond_spending` mặc định `false`).

**Hiện xong M2 farming + mua hàng trên báo (Daily Dirt).** Chưa storage OCR, chưa Decision Engine.

## Cài đặt

Cần Python 3.11+ và BlueStacks với **Settings → Advanced → Android Debug Bridge (ADB)** bật.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## M0 — Device controller

```bash
python -m app.main devices
python -m app.main info
python -m app.main screenshot
python -m app.main tap 500 300
python -m app.main swipe 800 400 300 400
```

## M1 — Vision

```bash
python -m app.main detect
python -m app.main detect tests/fixtures/farm.png
```

## M2 — Farming

```bash
python -m app.main fields
python -m app.main fields tests/fixtures/farm.png
python -m app.main harvest --limit 1
python -m app.main plant wheat --limit 1
python -m app.main pan right
python -m app.main pan reset
```

Harvest: swipe trên field `READY`, chụp lại, xác nhận không còn READY. Thất bại thì retry. Nếu hiện popup (kể cả dùng diamond) thì bấm X đóng, **không** tiêu diamond.

Plant: tap field `EMPTY`, tìm `seed_wheat.png` trên khay hạt, **kéo chéo** hạt qua ô đất (lưới isometric Hay Day, không swipe ngang), verify không còn EMPTY.

Camera: swipe tương đối theo kích thước màn hình (zone scan). `reset` hoàn tác các pan trong session.

## Newspaper — Daily Dirt

```bash
python -m app.main newspaper --limit 1
python -m app.main newspaper --mode browse --limit 8
python -m app.main shop-buy
python -m app.main shop-capture
python -m app.main detect tests/fixtures/newspaper.png
python -m app.main detect tests/fixtures/player_shop.png
```

Mở **cột báo** (mailbox + tờ báo trên farm, không phải `hud_shop`). **Hai chế độ:** `browse` chỉ lật trang Daily Dirt, khớp tin với wishlist, **không** vào nhà; `shop` vào từng tin có hàng wishlist → shop người chơi rồi mua. Shop roadside chỉ bán **coin**, nên bot tap item khớp wishlist, không dò coin/kim cương trên giá. Icon trên tin báo là in đen trắng, trong shop là màu — matcher item so **grayscale** (hình khối), không so màu. Nếu hiện popup thì đóng, không xác nhận. Icon wishlist lấy từ crop trên báo (`data/templates/item_*.png`); nếu shop không match thì crop lại icon trong crate từ web. Đóng shop **không** tin camera còn ở cột báo — kể cả khi đang ở farm người khác: `FIND_COLUMN` swipe chéo đến khi thấy cột, không tap nhà xanh về nhà. Lần đầu lưu toạ độ tap vào `data/column.json`; các lần sau tap thẳng, không screenshot lại. Cache miss thì xoá và tìm lại bằng vision.

## Web UI

```bash
python -m app.main web
```

Mở http://127.0.0.1:48721 để **điều khiển bot** (thu hoạch / trồng / mua báo, chụp màn hình, pan camera), **test shop đang mở** (nhận diện & mua, hoặc crop icon vào wishlist), **ghi/phát macro**, crop icon wishlist, chỉnh cây trồng và ADB. Diamond luôn khoá. Dừng web rồi mở lại nếu đang chạy phiên cũ.

`item_wheat.png` chưa có: thêm từ web (crop icon lúa mì trong shop) hoặc copy vào `data/templates/`.

## Đã xong

- M0: ADB, screenshot, tap/swipe, CLI, diamond=false
- M1: ScreenDetector FARM/POPUP/UNKNOWN, template HUD, debug overlay
- M2: FieldDetector EMPTY/GROWING/READY, harvest/plant + verify, CameraManager
- Newspaper: NEWSPAPER / PLAYER_SHOP, FIND_COLUMN, visit ad, BUY wishlist, CLOSE_SHOP
- Web UI: chạy/dừng bot, ghi/phát macro tap/swipe, config, wishlist item PNG, crops

## Chưa làm (cố ý)

M3+: barn/silo OCR, scheduler, production, animals, orders, Decision Engine, recovery.

## Test

```bash
pip install -e ".[dev]"
pytest -q
```

## Limitation

- Field HSV đo trên **1920x1080 Small UI**. Crop xanh non / cây trang trí có thể false positive.
- GROWING chỉ giữ blob gần READY/EMPTY — màn hình chỉ có crop đang lớn có thể bỏ sót.
- `item_*.png` crop từ tin báo (đen trắng) vẫn dùng được trong shop màu — matcher item chạy trên grayscale, không so màu.
- FIND_COLUMN dùng `newspaper_stand` + `newspaper_stand_zoom`. Zoom/góc khác hẳn hai crop vẫn có thể miss. Toạ độ cache (`data/column.json`) lệch zoom thì tap hụt — bot sẽ clear cache và search lại.
- Vào nhà load chậm; hết tin trên trang thì swipe trang báo.
- Chưa map world đầy đủ — chỉ pan zone.
- Template HUD/field/báo gắn resolution hiện tại.
- Macro gắn resolution và góc camera lúc ghi — zoom khác thì tap lệch, ghi lại cùng góc nhìn.
- Package name game để trống trong config.
