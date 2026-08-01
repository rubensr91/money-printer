# MoneyPrinterV2 — Setup Guide

Guía para poner en marcha el pipeline de clips de YouTube/Twitch/Instagram → TikTok vía Telegram en un PC nuevo. Un LLM puede seguir estos pasos para instalar, configurar y arrancar todo.

**Funcionalidades principales:**
- Descarga de YouTube (con auto-captions), Twitch, Instagram y enlaces directos .mp4
- DeepSeek selecciona momentos virales + genera descripción y hashtags automáticamente
- Render panorámico 9:16 con fondos personalizables (pixelado, colores, negro, blanco) o horizontal 16:9
- Face tracking dinámico (sigue al hablante)
- Render acelerado por GPU (NVENC/AMF) con fallback CPU
- Cola de trabajos persistente en SQLite (sobrevive reinicios)
- Playlists de YouTube → procesamiento en lote
- Modo automático: genera y sube a TikTok sin interacción
- A/B testing de variantes de clips
- Web UI local para monitorear cola, historial y logs
- Subida a TikTok con descripción y hashtags generados

---

## 1. Prerrequisitos

Instalar si no están presentes:

**Windows:**
```powershell
# Python 3.12+ (descargar de python.org, marcar "Add to PATH")
# FFmpeg (descargar de ffmpeg.org o gyan.dev — con soporte NVENC, añadir a PATH)
# Git (git-scm.com)

# Verificar:
python --version
ffmpeg -version   # debe listar h264_nvenc entre los encoders
git --version
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg git
# Para GPU NVIDIA (opcional): ffmpeg con libnvidia-encode
sudo apt install -y ffmpeg nvidia-cuda-toolkit
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

# Instalar dependencias (incluye flask, opencv, openai, yt-dlp, python-telegram-bot)
pip install -r requirements.txt

# Instalar playwright (subida a TikTok)
python -m playwright install chromium
```

Si `requirements.txt` no está actualizado, instalar manualmente:
```bash
pip install moviepy openai python-telegram-bot requests pillow flask opencv-python yt-dlp
```

**Nota face tracking:** OpenCV 5 no incluye el XML de haarcascades. Copiar el archivo `haarcascade_frontalface_default.xml` a `.mp/` (se descarga de opencv/data en GitHub) o a `assets/`. El pipeline lo busca en: `.mp/`, `assets/`, y `cv2.data.haarcascades`.

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
- `threads`: núcleos de CPU para el render fallback (4-8 según la máquina)

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

Esto descarga el video, busca momentos virales con DeepSeek (generando descripción y hashtags) y crea los clips en `.mp/`. Los videos se cachean por ID — repetir con el mismo video no re-descarga.

---

## 7. Arrancar sistemas (bot + web UI)

```bash
# Con el venv activado, desde la raíz del proyecto
python start_bot_launcher.py
```

Esto arranca:
1. **Bot de Telegram** — escucha URLs y comandos. Logs en `bot_output.log`
2. **Web UI** — dashboard local en `http://127.0.0.1:5050` (cola, historial, logs en tiempo real)

Para pararlo: `taskkill //F //IM python.exe` (Windows) / `pkill -f telegram_bot` (Linux).

---

## 8. Comandos del bot de Telegram

| Comando | Efecto |
|---|---|
| `/help` | Lista todos los comandos |
| `/config` | Ver configuración actual |
| `/clips <n>` | Nº de clips por defecto (1-5) |
| `/duracion <min> <max>` | Rango de duración (segundos) |
| `/fondo <color|pixel|none>` | Fondo por defecto |
| `/texto <frase>` / `/texto off` | Texto overlay por defecto |
| `/horizontal` | Modo 16:9 sin letterbox |
| `/panoramico` | Modo 9:16 fondo pixelado |
| `/queue` | Ver cola de trabajos |
| `/cancel <id>` | Cancelar trabajo pendiente |
| `/historial` | Últimos trabajos |
| `/auto on\|off` | Subida automática a TikTok |
| `/abtest on\|off\|results` | A/B testing de variantes |
| `/reset` | Restaurar defaults |

**Prioridad de configuración:** instrucciones en el mensaje > comandos (`/clips`, `/fondo`...) > defaults de fábrica.

---

## 9. Uso del bot (producción de clips)

Enviar un mensaje con el enlace + instrucciones opcionales:

```
https://www.youtube.com/watch?v=xxx -> horizontal, 1 clip de 30 segundos, fondo blanco
```

**Plataformas soportadas:**

| Plataforma | Ejemplo | Captions |
|---|---|---|
| YouTube | `youtube.com/watch?v=...` o `youtu.be/...` | ✅ auto-captions |
| Twitch VOD | `twitch.tv/videos/...` | ❌ (fallback time-split) |
| Instagram | `instagram.com/...` | ❌ |
| Enlace directo | `https://.../video.mp4` | ❌ |
| Playlist YouTube | `youtube.com/playlist?list=...` | ✅ (encola todos) |

**Instrucciones disponibles:**

| Frase | Efecto |
|---|---|
| `1 clip de 30 segundos` | Número de clips y duración |
| `horizontal` / `sin fondo` / `16:9` | Clip horizontal sin letterbox |
| `fondo blanco` / `fondo negro` | Fondo de color sólido |
| `fondo rojo` / `fondo azul` / etc | Fondo de color personalizado |
| `texto "tu frase"` | Texto incrustado en la banda inferior |
| `dinamico` | Face tracking: crop sigue al hablante |
| (sin instrucciones) | 3 clips de 20-60s, fondo pixelado del video |

**Colores disponibles:** rojo, azul, verde, amarillo, naranja, morado, rosa, gris, cian, marrón, turquesa, dorado, plata, beige, coral, blanco, negro (+ equivalentes en inglés).

**Flujo con cola:**
1. Envías URL → respuesta `📥 En cola (job #N)`
2. Worker procesa en background (descarga → DeepSeek → render → envío)
3. Recibes cada clip con botones `▶ Subir a TikTok` / `⏭ Saltar`
4. Cada clip incluye caption con descripción viral + hashtags generados por DeepSeek

---

## 10. Subida a TikTok

El bot tiene botones inline `▶ Subir a TikTok` después de cada clip, o modo automático con `/auto on`. Requiere:

- `tiktok_cookies.json` en `.mp/` (exportar cookies de sesión de TikTok con una extensión de navegador)
- Playwright con Chromium instalado (paso 3)

Con `/auto on`, los clips se suben automáticamente sin botones, usando la descripción y hashtags generados. Con `/abtest on`, cada clip genera además una variante B (fondo/texto alternativos) que también se sube, para comparar rendimiento con `/abtest results`.

---

## 11. Render y GPU

El pipeline detecta automáticamente el mejor encoder disponible:
1. **h264_nvenc** (GPU NVIDIA) — hasta ~50-60 fps
2. **h264_amf** (GPU AMD) — hasta ~50-60 fps
3. **h264_qsv** (GPU Intel) — ~5 fps
4. **libx264** (CPU) — ~2-5 fps (fallback)

### FFmpeg compatible con tu driver NVIDIA

Los ffmpeg modernos (8.x) exigen driver NVIDIA ≥551.76 (nvenc API 12.2). Con
drivers antiguos (p.ej. 546.18, API 12.1) NVENC falla con "Invalid argument".

El bot usa un **ffmpeg 6.1.1 propio en `.mp/tools/ffmpeg.exe`** que sí funciona
con drivers antiguos. Se copia ahí manualmente:

```
mkdir .mp/tools
# descargar https://github.com/GyanD/codexffmpeg/releases/download/6.1.1/ffmpeg-6.1.1-essentials_build.zip
# copiar bin/ffmpeg.exe y bin/ffprobe.exe a .mp/tools/
```

El código antepone `.mp/tools/` al PATH y fuerza moviepy a usar ese binario, así
que funciona aunque el ffmpeg del sistema sea otro. Sin `.mp/tools/`, cae al
ffmpeg del sistema (que puede fallar con NVENC si el driver es viejo).

### Render a media resolución

El cuello de botella real no es el encoder sino el bucle de composición en
Python (frames a 1080x1920). El pipeline compone a **540x960** (4x menos
píxeles/frame) y ffmpeg hace el upscale a 1080x1920 durante el encode.
El fondo pixelado oculta la pérdida. Resultado: render ~2.2x más rápido en
todos los encoders, sin perder resolución final.

Verificación: `ffmpeg -encoders | findstr nvenc` debe listar `h264_nvenc`.
Con GPU, un clip de 30s se renderiza en ~1 minuto en vez de ~3 minutos.

---

## 12. Web UI

Dashboard local en `http://127.0.0.1:5050`:
- **Cola**: trabajos pendientes/procesando, botón ✕ para cancelar
- **Historial**: últimos 15 trabajos con estado
- **Logs**: últimas 100 líneas de `bot_output.log`

Auto-refresh cada 3-5s vía htmx. Solo accesible desde localhost.

---

## 13. Arquitectura de módulos

```
src/
├── telegram_bot.py      — Bot + comandos + worker de cola
├── tiktok_clips.py       — Pipeline: descarga → DeepSeek → render (multi-plataforma)
├── job_queue.py          — Cola SQLite persistente (.mp/jobs.db)
├── ab_testing.py         — Variantes A/B + registro de resultados
├── face_tracker.py       — Detección de caras OpenCV + trayectoria suave
├── web_ui.py             — Dashboard Flask (127.0.0.1:5050)
├── bot_config.py         — Settings por chat (JSON)
├── llm_provider.py       — Cliente OpenAI → DeepSeek
├── progress_reporter.py  — Progreso cada 5s vía Telegram
├── tiktok_uploader.py    — Subida a TikTok (Playwright)
├── config.py             — Lee config.json
start_bot_launcher.py     — Arranca bot + web UI
```

---

## 14. Solución de problemas

| Error | Causa | Solución |
|---|---|---|
| `yt-dlp: command not found` | yt-dlp no instalado | `pip install yt-dlp` |
| `MoviePy - ffmpeg not found` | FFmpeg no en PATH | Instalar ffmpeg y añadir a PATH |
| `DeepSeek API error 401` | API key inválida | Revisar `config.json` |
| `Telegram sendMessage 401` | Bot token inválido | Reconfigurar con `setup_telegram()` |
| `charmap codec can't encode` | Emoji en Windows | Ya manejado (encoding seguro), si persiste: terminal UTF-8 |
| `Permission denied (publickey)` | Sin clave SSH en GitHub | Usar HTTPS o configurar SSH key |
| Video no tiene subtítulos | Plataforma sin auto-captions | El pipeline usa fallback time-based split |
| OpenCV no detecta caras | Falta haarcascade XML en `.mp/` o `assets/` | Copiar `haarcascade_frontalface_default.xml` allí |
| `Invalid font Arial` | Font de overlay no encontrada | El pipeline busca fuentes en `fonts/` y Windows Fonts automáticamente |
| NVENC no aparece en ffmpeg | FFmpeg sin soporte GPU | Instalar ffmpeg de gyan.dev (Windows) o con libnvidia-encode (Linux) |
| NVENC falla "Invalid argument" | Driver NVIDIA viejo (<551.76) con ffmpeg 8.x | Usar el ffmpeg 6.1.1 bundle en `.mp/tools/` (ver sección 11) o actualizar driver |

---

## 15. Actualizar el código

```bash
git pull origin main
# Si hay cambios en dependencias:
pip install -r requirements.txt
# Reiniciar sistemas
taskkill //F //IM python.exe && python start_bot_launcher.py
```

---

## 16. Variables de entorno alternativas (opcional)

En vez de `config.json`, se pueden usar variables de entorno:

```bash
export DEEPSEEK_API_KEY="sk-xxx"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
```

El código prioriza el JSON; si quieres usar env vars directamente modifica `src/config.py`.
