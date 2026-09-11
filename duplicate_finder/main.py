from __future__ import annotations

from .app import DuplicateFinderApp


def main() -> None:
    app = DuplicateFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
