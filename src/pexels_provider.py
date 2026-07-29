import os
import uuid
import requests

from config import get_pexels_api_key, ROOT_DIR


def search_videos(query: str, per_page: int = 5, orientation: str = "portrait") -> list[dict]:
    """
    Search videos on Pexels.

    Args:
        query: Search query
        per_page: Number of results (max 80)
        orientation: landscape, portrait, or square

    Returns:
        List of video dicts with keys: id, url, duration, video_files, etc.
    """
    api_key = get_pexels_api_key()
    if not api_key:
        raise RuntimeError("Pexels API key not configured")

    headers = {"Authorization": api_key}
    params = {
        "query": query,
        "per_page": min(per_page, 80),
        "orientation": orientation,
    }

    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("videos", [])


def search_photos(query: str, per_page: int = 10, orientation: str = "portrait") -> list[dict]:
    """
    Search photos on Pexels.

    Args:
        query: Search query
        per_page: Number of results (max 80)
        orientation: landscape, portrait, or square

    Returns:
        List of photo dicts with keys: id, url, src (with original, large, etc.)
    """
    api_key = get_pexels_api_key()
    if not api_key:
        raise RuntimeError("Pexels API key not configured")

    headers = {"Authorization": api_key}
    params = {
        "query": query,
        "per_page": min(per_page, 80),
        "orientation": orientation,
    }

    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("photos", [])


def download_file(url: str, output_dir: str = None) -> str:
    """
    Download a file from a URL to .mp directory.

    Args:
        url: URL to download
        output_dir: Optional output directory

    Returns:
        Path to downloaded file
    """
    if output_dir is None:
        output_dir = os.path.join(ROOT_DIR, ".mp")

    os.makedirs(output_dir, exist_ok=True)

    ext = url.split("?")[0].split(".")[-1] or "mp4"
    filename = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join(output_dir, filename)

    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    with open(filepath, "wb") as f:
        f.write(resp.content)

    return filepath
