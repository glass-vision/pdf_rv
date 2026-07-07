import uvicorn
from pathlib import Path

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    app_dir = Path(__file__).resolve().parent
    data_dir = settings.app_data_dir.resolve()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.app_env == "development",
        reload_dirs=[str(app_dir)],
        reload_excludes=[str(data_dir)],
    )


if __name__ == "__main__":
    main()
