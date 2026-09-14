from __future__ import annotations

import argparse
import sys

from app.config import load_config
from app.actions.farming import FarmingActions
from app.actions.shop import NewspaperActions
from app.controller.adb import AdbClient, DeviceError
from app.controller.camera import CameraManager
from app.controller.device import DeviceController
from app.storage.logger import setup_logging
from app.vision.fields import FieldDetector
from app.vision.newspaper import NewspaperDetector
from app.vision.overlay import save_overlay
from app.vision.screen import GameScreen, ScreenDetection, ScreenDetector
from app.vision.template_matcher import TemplateMatcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hay Day bot CLI (newspaper + M2 farming)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="List adb devices")
    sub.add_parser("info", help="Connect and print device resolution")
    sub.add_parser("screenshot", help="Capture screen to screenshots/")
    sub.add_parser("back", help="Android back key")
    sub.add_parser("home", help="Android home key")

    tap = sub.add_parser("tap", help="Tap at x y")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)

    swipe = sub.add_parser("swipe", help="Swipe x1 y1 x2 y2 [duration_ms]")
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("duration", type=int, nargs="?", default=None)

    launch = sub.add_parser("launch", help="Launch app by package name")
    launch.add_argument("package")

    detect = sub.add_parser("detect", help="Classify screen from image or live screenshot")
    detect.add_argument("image", nargs="?", help="PNG path; omit to capture from device")

    fields = sub.add_parser("fields", help="Detect field plots (EMPTY/GROWING/READY)")
    fields.add_argument("image", nargs="?", help="PNG path; omit to capture from device")

    harvest = sub.add_parser("harvest", help="Harvest READY fields with verify")
    harvest.add_argument("--limit", type=int, default=1)

    plant = sub.add_parser("plant", help="Plant EMPTY fields with verify")
    plant.add_argument("crop", nargs="?", default="wheat")
    plant.add_argument("--limit", type=int, default=1)

    pan = sub.add_parser("pan", help="Pan farm camera by zone swipe")
    pan.add_argument("direction", choices=["left", "right", "up", "down", "reset"])

    newspaper = sub.add_parser("newspaper", help="Browse Daily Dirt ads or visit shops to buy")
    newspaper.add_argument("--limit", type=int, default=1)
    newspaper.add_argument(
        "--mode",
        choices=["browse", "shop"],
        default="shop",
        help="browse = chỉ lật tin trên báo; shop = ghé shop rồi mua",
    )

    sub.add_parser("shop-buy", help="Buy wishlist items on the already-open player shop")
    sub.add_parser("shop-capture", help="Crop priced crate icons into the wishlist")

    web = sub.add_parser("web", help="Web UI for config, wishlist items, and crops")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=48721)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    setup_logging(config)
    try:
        return _dispatch(args, config)
    except (DeviceError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, config) -> int:
    if args.cmd == "devices":
        client = AdbClient(config.adb_bin)
        client.start_server()
        rows = client.devices()
        if not rows:
            print("No devices.")
            return 0
        for serial, state in rows:
            print(f"{serial}\t{state}")
        return 0

    if args.cmd == "detect":
        return _detect(args, config)

    if args.cmd == "fields":
        return _fields(args, config)

    if args.cmd == "web":
        from app.web.server import serve

        serve(args.host, args.port)
        return 0

    device = DeviceController(config)
    device.connect()

    if args.cmd == "info":
        width, height = device.resolution()
        print(f"serial\t{device.serial}")
        print(f"adb\t{device.adb.adb_bin}")
        print(f"resolution\t{width}x{height}")
        return 0

    if args.cmd == "screenshot":
        path = device.save_screenshot()
        print(path)
        return 0

    if args.cmd == "tap":
        device.tap(args.x, args.y)
        return 0

    if args.cmd == "swipe":
        device.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)
        return 0

    if args.cmd == "back":
        device.back()
        return 0

    if args.cmd == "home":
        device.home()
        return 0

    if args.cmd == "launch":
        device.launch_app(args.package)
        return 0

    if args.cmd == "harvest":
        result = FarmingActions(device, config).harvest_ready_fields(limit=args.limit)
        print(f"ok\t{result.success}")
        print(f"action\t{result.action.type}\t{result.action.target}")
        if result.error:
            print(f"error\t{result.error}")
        return 0 if result.success else 1

    if args.cmd == "plant":
        result = FarmingActions(device, config).plant_empty_fields(
            crop=args.crop, limit=args.limit
        )
        print(f"ok\t{result.success}")
        print(f"action\t{result.action.type}\t{result.action.target}")
        if result.error:
            print(f"error\t{result.error}")
        return 0 if result.success else 1

    if args.cmd == "pan":
        camera = CameraManager(device)
        if args.direction == "reset":
            camera.reset_camera()
        else:
            getattr(camera, f"pan_{args.direction}")()
        return 0

    if args.cmd == "newspaper":
        result = NewspaperActions(device, config).shop_from_newspaper(
            limit=args.limit, mode=args.mode
        )
        print(f"ok\t{result.success}")
        print(f"action\t{result.action.type}\t{result.action.target}")
        if result.error:
            print(f"error\t{result.error}")
        return 0 if result.success else 1

    if args.cmd == "shop-buy":
        result = NewspaperActions(device, config).buy_open_shop(limit=1)
        print(f"ok\t{result.success}")
        print(f"action\t{result.action.type}\t{result.action.target}")
        if result.error:
            print(f"error\t{result.error}")
        return 0 if result.success else 1

    if args.cmd == "shop-capture":
        result = NewspaperActions(device, config).capture_open_shop()
        print(f"ok\t{result.success}")
        print(f"action\t{result.action.type}\t{result.action.target}")
        if result.error:
            print(f"error\t{result.error}")
        return 0 if result.success else 1

    return 1


def _detect(args: argparse.Namespace, config) -> int:
    if args.image:
        source = args.image
    else:
        device = DeviceController(config)
        device.connect()
        source = device.screenshot()
    matcher = TemplateMatcher(threshold=config.template_threshold)
    result = ScreenDetector(matcher).detect(source)
    if result.screen is GameScreen.FARM:
        extra = FieldDetector().detect(source)
        stand = NewspaperDetector().find_stand(source)
        objects = [*result.objects, *extra]
        if stand is not None:
            objects = [stand, *objects]
        result = ScreenDetection(result.screen, result.confidence, objects)
    elif result.screen is GameScreen.NEWSPAPER:
        ads = NewspaperDetector().find_ads(source)
        result = ScreenDetection(
            result.screen, result.confidence, [*result.objects, *ads]
        )
    print(f"screen\t{result.screen.value}")
    print(f"confidence\t{result.confidence:.3f}")
    for obj in result.objects:
        print(
            f"object\t{obj.type}\t{obj.state}\t"
            f"{obj.x},{obj.y} {obj.width}x{obj.height}\t{obj.confidence:.3f}"
        )
    if config.debug:
        overlay = save_overlay(source, result)
        print(f"overlay\t{overlay}")
    return 0


def _fields(args: argparse.Namespace, config) -> int:
    if args.image:
        source = args.image
    else:
        device = DeviceController(config)
        device.connect()
        source = device.screenshot()
    fields = FieldDetector().detect_fields(source)
    print(f"fields\t{len(fields)}")
    for field in fields:
        cx, cy = field.center
        print(
            f"field\t{field.id}\t{field.state.value}\t"
            f"{field.x},{field.y} {field.width}x{field.height}\t"
            f"center={cx},{cy}\t{field.confidence:.3f}"
        )
    if config.debug:
        screen = ScreenDetector().detect(source)
        combined = ScreenDetection(
            screen.screen,
            screen.confidence,
            [*screen.objects, *[f.to_object() for f in fields]],
        )
        overlay = save_overlay(source, combined)
        print(f"overlay\t{overlay}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
