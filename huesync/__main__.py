"""Entry point — allows running the package with `python -m huesync`."""

from .app import HueSyncApp


def main() -> None:
    HueSyncApp().run()


if __name__ == "__main__":
    main()
