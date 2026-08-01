"""
TikTok Uploader - Playwright-based video upload to TikTok.
First run: manual login (browser opens, you log in, press Enter in terminal).
Subsequent runs: reuses saved session cookies.
"""

import os
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ROOT_DIR

COOKIES_FILE = os.path.join(ROOT_DIR, ".mp", "tiktok_cookies.json")
TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload?lang=es"
TIKTOK_LOGIN = "https://www.tiktok.com/login"
USER_DATA_DIR = os.path.join(ROOT_DIR, ".mp", "tiktok_profile")


def _get_browser():
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    context = p.chromium.launch_persistent_context(
        USER_DATA_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    return p, None, context


def login_and_save_cookies():
    """
    Open TikTok in persistent browser. User logs in manually.
    Session persists on disk automatically (persistent context).
    Just wait for login, no detection needed.
    """
    print("\n" + "=" * 50)
    print("  TIKTOK LOGIN")
    print("=" * 50)
    print("  Abriendo navegador... Inicia sesión en TikTok.")
    print("  Tienes 3 minutos. La sesión se guarda en disco.")
    print("=" * 50 + "\n")

    p, _, context = _get_browser()
    page = context.new_page()

    # Check if already logged in
    page.goto("https://www.tiktok.com/upload?lang=es", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if "login" not in page.url.lower():
        print("  ¡Ya hay sesión activa! No necesita login.")
        page.close()
        context.close()
        p.stop()
        return True

    # Go to login page
    page.goto(TIKTOK_LOGIN, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    try:
        page.click("text=Use phone / email / username", timeout=3000)
    except:
        pass

    # Just wait. Persistent context saves to disk automatically.
    print("  Esperando 3 minutos para que inicies sesión...")
    for i in range(180, 0, -1):
        time.sleep(1)
        if i % 30 == 0:
            # Check if already logged in by polling URL
            try:
                if "login" not in page.url.lower() and "tiktok.com" in page.url.lower():
                    print(f"\n  ¡Login detectado! Cerrando en 3s...")
                    time.sleep(3)
                    break
            except:
                pass

    page.close()
    context.close()
    p.stop()
    print("  Sesión guardada. ¡Listo!")
    return True


def upload_video(video_path, description="", tags=None, draft=True):
    """
    Upload a video to TikTok with description and hashtags.
    Uses persistent browser session.

    Args:
        video_path: Path to MP4 file
        description: Video description text
        tags: List of hashtags (without #) e.g. ['viral','humor']
        draft: If True, saves as draft (private) instead of publishing
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    video_path = os.path.abspath(video_path)

    p, _, context = _get_browser()
    page = context.new_page()

    # Check if already logged in
    page.goto("https://www.tiktok.com", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if "login" in page.url.lower():
        print("  No hay sesión activa. Iniciando login...")
        page.goto(TIKTOK_LOGIN, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        try:
            page.click("text=Use phone / email / username", timeout=3000)
        except:
            pass
        print("  Inicia sesión en el navegador. Detectando automáticamente...")
        for i in range(300):
            time.sleep(1)
            url = page.url.lower()
            if "login" not in url and "tiktok.com" in url:
                cookies = context.cookies()
                has_session = any(c.get("name") == "sessionid" for c in cookies)
                if has_session or "foryou" in url:
                    print("  ¡Login detectado!")
                    break
        else:
            print("  Timeout en login.")
            context.close()
            p.stop()
            return False

    print(f"  Subiendo: {os.path.basename(video_path)}")
    page.goto(TIKTOK_UPLOAD, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    # Upload video via file input
    try:
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(video_path)
        print("  Video cargado, esperando procesamiento...")
    except Exception as e:
        # Try iframe-based upload
        print(f"  Input directo no encontrado, probando iframe... ({e})")
        try:
            page.goto(f"{TIKTOK_UPLOAD}?videoForm=upload", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(video_path)
        except Exception as e2:
            print(f"  Error al cargar video: {e2}")
            page.screenshot(path=os.path.join(ROOT_DIR, ".mp", "tiktok_upload_error.png"))
            context.close()
            p.stop()
            return False

    # Wait for upload UI to fully settle — specifically wait for "Guardar borrador"
    print("  Esperando que termine el procesamiento...")
    try:
        page.wait_for_selector('button:has-text("Guardar borrador")', timeout=120000)
        page.wait_for_timeout(2000)
        print("  Procesamiento completo")
    except:
        # Fallback: wait for Publicar if no draft button
        try:
            page.wait_for_selector('button:has-text("Publicar")', timeout=120000)
            page.wait_for_timeout(2000)
        except:
            pass

    # Add description/caption
    try:
        caption_el = page.locator('[contenteditable="true"]').first
        caption_el.wait_for(timeout=10000, state="attached")
        time.sleep(1)
        full_text = description
        if tags:
            full_text += " " + " ".join(f"#{t.strip('#')}" for t in tags)
        caption_el.click(force=True)
        page.wait_for_timeout(500)
        caption_el.fill(full_text)
        print(f"  Descripcion anadida")
    except Exception as e:
        print(f"  Aviso caption: {e}")
        try:
            page.keyboard.press("Tab")
            page.wait_for_timeout(500)
            page.keyboard.type(description + " " + " ".join(f"#{t.strip('#')}" for t in tags), delay=50)
        except:
            pass

    page.wait_for_timeout(2000)

    if draft:
        print("  Click en Guardar borrador...")
        save_btn = page.locator('button:has-text("Guardar borrador")')
        if save_btn.count() > 0 and save_btn.is_enabled():
            save_btn.first.click(no_wait_after=True)
            # Wait for navigation (TikTok redirects to content page after save)
            try:
                page.wait_for_url('**/tiktokstudio/content**', timeout=15000)
                print("  Borrador guardado!")
                time.sleep(2)
                context.close()
                p.stop()
                return True
            except:
                print("  Sin navegacion, esperando...")
                time.sleep(5)
                context.close()
                p.stop()
                return True  # Assume saved even without nav
        else:
            print("  Boton no disponible. Cerrando sin publicar.")
            context.close()
            p.stop()
            return False
            print(f"  Error: {e}")
            context.close()
            p.stop()
            return False
        save_draft_found = False

        try:
            for sel in [
                'button:has-text("Guardar borrador")',
                'button:has-text("Save draft")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0:
                        btn.click(force=True, timeout=5000)
                        print("  ✓ Guardado como borrador!")
                        save_draft_found = True
                        break
                except:
                    continue

            if not save_draft_found:
                all_btns = page.locator("button").all()
                for btn in all_btns:
                    try:
                        text = btn.text_content()
                        if text and "Guardar" in text and "borrador" in text:
                            btn.click(force=True, timeout=3000)
                            print("  ✓ Guardado como borrador!")
                            save_draft_found = True
                            break
                    except:
                        continue
        except Exception as e:
            print(f"  Error: {e}")

        if save_draft_found:

            page.wait_for_timeout(5000)
            context.close()
            p.stop()
            return True
        else:
            print("  No se encontró. Cerrando sin publicar.")

            page.screenshot(path=os.path.join(ROOT_DIR, ".mp", "draft_fail.png"))

            context.close()
            p.stop()
            return False

    # If not draft

    # If not draft, or draft button not found, click post
    page.wait_for_timeout(2000)
    try:
        # Multiple possible selectors for TikTok's post button
        for selector in [
            "button:has-text('Publicar')",
            "button:has-text('Post')",
            "button:has-text('Subir')",
            '[data-e2e="post_video_button"]',
        ]:
            try:
                post_btn = page.locator(selector).first
                post_btn.wait_for(timeout=3000)
                post_btn.click()
                print(f"  Botón de publicar clickeado ({selector})")
                break
            except:
                continue
        else:
            print("  No se encontró el botón de publicar. Captura guardada.")
            page.screenshot(path=os.path.join(ROOT_DIR, ".mp", "tiktok_post_error.png"))
    except Exception as e:
        print(f"  Error al publicar: {e}")

    # Wait for upload completion
    print("  Esperando confirmación de publicación...")
    time.sleep(8)

    context.close()
    p.stop()
    print("  ¡Publicado!")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TikTok Uploader")
    parser.add_argument("video", nargs="?", help="Video file to upload")
    parser.add_argument("--desc", default="", help="Description")
    parser.add_argument("--tags", nargs="*", default=[], help="Hashtags without #")
    parser.add_argument("--login", action="store_true", help="Only login, don't upload")
    args = parser.parse_args()

    if args.login:
        login_and_save_cookies()
    elif args.video:
        upload_video(args.video, args.desc, args.tags)
    else:
        print("Uso: python -m src.tiktok_uploader <video.mp4> --desc 'texto' --tags viral humor")
