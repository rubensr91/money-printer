# MoneyPrinterV2 — Setup Guide

Guía para poner en marcha el pipeline de clips de YouTube → TikTok vía Telegram en un PC nuevo. Un LLM puede seguir estos pasos para instalar, configurar y arrancar todo.

---

## 1. Prerrequisitos

Instalar si no están presentes:

**Windows:**
```powershell
# Python 3.12+ (descargar de python.org, marcar "Add to PATH")
# FFmpeg (descargar de ffmpeg.org, añadir a PATH)
# Git (git-scm.com)

# Verificar:
python --version
ffmpeg -version
git --version
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg git
python3 --version
```

**macOS:**
```bash
brew install python@3.12 ffmpeg git
```

---

## 2. Clonar el repositorio

```bash
git clone git@github.com:rubensr91/money-printer.git
cd money-printer
```

O por HTTPS:
```bash
git clone https://github.com/rubensr91/money-printer.git
```

---

## 3. Entorno virtual + dependencias

```bash
# Crear venv
python -m venv venv

# Activar (Windows):
venv\Scripts\activate
# Activar (Linux/macOS):
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Instalar yt-dlp (descarga de YouTube)
pip install yt-dlp

# Instalar playwright (subida a TikTok)
python -m playwright install chromium
```

Si `requirements.txt` no está actualizado, instalar manualmente:
```bash
pip install moviepy openai python-telegram-bot requests pillow
```

---

## 4. Configuración (config.json)

Crear `config.json` en la raíz del proyecto con las claves necesarias:

```json
{
  "deepseek_api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "deepseek_base_url": "https://api.deepseek.com",
  "deepseek_model": "deepseek-chat",
  "threads": 4
}
```

- `deepseek_api_key`: clave API de DeepSeek (obtener en platform.deepseek.com)
- `deepseek_model`: modelo a usar (recomendado `deepseek-chat` o `deepseek-v4-flash`)
- `threads`: núcleos de CPU para el render (4-8 según la máquina)

---

## 5. Configuración Telegram

Ejecutar una vez para guardar las credenciales del bot:

```bash
# Con el venv activado
python -c "
import sys
sys.path.insert(0, 'src')
from telegram_notify import setup_telegram
setup_telegram('TU_BOT_TOKEN', 'TU_CHAT_ID')
"
```

- `TU_BOT_TOKEN`: token del bot de Telegram (obtener de @BotFather)
- `TU_CHAT_ID`: tu ID de chat (enviar `/start` al bot y luego usar `get_chat_id` o mirar en getUpdates)

Esto guarda `telegram.json` en `.mp/telegram.json` (gitignored, no se sube al repo).

---

## 6. Probar el pipeline offline (sin bot)

```bash
# Con el venv activado
python src/tiktok_clips.py "https://www.youtube.com/watch?v=VIDEO_ID" --clips 2 --min 20 --max 60
```

Esto descarga el video, busca momentos virales con DeepSeek y genera los clips en `.mp/`.

---

## 7. Arrancar el bot de Telegram

```bash
# Con el venv activado, desde la raíz del proyecto
python start_bot_launcher.py
```

El bot queda corriendo. Los logs van a `bot_output.log`.

Para pararlo: `Ctrl+C` o `taskkill //F //IM python.exe` (Windows) / `pkill -f telegram_bot` (Linux).

---

## 8. Uso del bot

Enviar por Telegram un mensaje con el enlace de YouTube. Opcionalmente, instrucciones:

```
https://www.youtube.com/watch?v=xxx -> horizontal, 1 clip de 30 segundos, fondo blanco
```

**Instrucciones disponibles:**

| Frase | Efecto |
|---|---|
| `1 clip de 30 segundos` | Número de clips y duración |
| `horizontal` / `sin fondo` / `16:9` | Clip horizontal sin letterbox |
| `fondo blanco` / `fondo negro` | Fondo de color sólido |
| `fondo rojo` / `fondo azul` / etc | Fondo de color personalizado |
| `texto "tu frase"` | Texto incrustado en la banda inferior |
| (sin instrucciones) | 3 clips de 20-60s, fondo pixelado del video |

---

## 9. Subida a TikTok

El bot tiene botones inline `▶ Subir a TikTok` después de cada clip. Requiere:

- `tiktok_cookies.json` en `.mp/` (exportar cookies de sesión de TikTok con una extensión de navegador)
- Playwright con Chromium instalado (paso 3)

Si no necesitas subida automática, puedes ignorar los botones y descargar los clips manualmente desde `.mp/`.

---

## 10. Solución de problemas

| Error | Causa | Solución |
|---|---|---|
| `yt-dlp: command not found` | yt-dlp no instalado | `pip install yt-dlp` |
| `MoviePy - ffmpeg not found` | FFmpeg no en PATH | Instalar ffmpeg y añadir a PATH |
| `DeepSeek API error 401` | API key inválida | Revisar `config.json` |
| `Telegram sendMessage 401` | Bot token inválido | Reconfigurar con `setup_telegram()` |
| `charmap codec can't encode` | Emoji en Windows | Ya manejado (encoding seguro), si persiste: terminal UTF-8 |
| `Permission denied (publickey)` | Sin clave SSH en GitHub | Usar HTTPS o configurar SSH key |
| Video no tiene subtítulos | YouTube no generó auto-captions en español | El pipeline usa fallback time-based split |

---

## 11. Actualizar el código

```bash
git pull origin main
# Si hay cambios en dependencias:
pip install -r requirements.txt
# Reiniciar bot
```

---

## 12. Variables de entorno alternativas (opcional)

En vez de `config.json`, se pueden usar variables de entorno:

```bash
export DEEPSEEK_API_KEY="sk-xxx"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
```

El código prioriza el JSON; si quieres usar env vars directamente modifica `src/config.py`.
