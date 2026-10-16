# Shorts-pipeline

Maakt YouTube Shorts over ruimtevaart en sterrenkunde uit rechtenvrij NASA-beeld, met alleen tekst op beeld. Jij keurt elke Short goed; daarna plant de pipeline hem in op YouTube.

```
ingest ─► scenes ─► embed ─► make (hero shot ─► script ─► feitencheck ─► beeld per tekstblok ─► render) ─► review ─► upload ─► stats
```

## Eenmalig installeren (Windows)

1. In PowerShell: `winget install Python.Python.3.12 Git.Git Gyan.FFmpeg`
2. Claude Code: `irm https://claude.ai/install.ps1 | iex`, daarna één keer `claude` starten en inloggen.
3. Repo ophalen en setup draaien (nieuwe PowerShell openen na stap 1):
   ```powershell
   cd C:\Users\roela\OneDrive\Documenten\shorts
   git clone https://github.com/roelandsteur1-blip/shorts.git pipeline
   cd pipeline
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```
   `setup.ps1` installeert PyTorch met CUDA, de overige pakketten en het lettertype, en draait `shorts check`. Alles moet `[OK]` zijn.

Database en renders staan in `C:\shorts-data` (buiten OneDrive). De gedownloade video's (het grote deel) kunnen op een andere schijf: zet in `config/local.toml` bijvoorbeeld `footage_dir = "D:/shorts-footage"` en draai één keer `python -m shorts move-footage` om bestaande video's te verhuizen. Downloaden stopt als er minder dan `min_free_gb` (standaard 20 GB) vrij is.

## Dagelijks gebruik

Activeer eerst de omgeving: `.\.venv\Scripts\activate`

| Stap | Commando | Wat het doet |
| :-- | :-- | :-- |
| 1 | `python -m shorts ingest --per-term 5` | Downloadt NASA-video's voor de zoektermen in `config/channels/space.toml`, haalt het geluid eraf. Video's met een credit van derden (SpaceX, ESA, "courtesy"…) worden overgeslagen. |
| 1 | `python -m shorts scenes` | Knipt in scènes, gooit zwart/titelkaarten weg, bepaalt waar de 9:16-uitsnede moet. |
| 1 | `python -m shorts embed` | Maakt scènes doorzoekbaar met CLIP op je GPU (geen Claude-gebruik). |
| – | `python -m shorts search "saturn rings"` | Test de beeldzoeker. |
| 2–5 | `python -m shorts make -n 5` | Kiest 5 hero shots, laat Claude er scripts bij schrijven (1 aanroep per 5), controleert de feiten in een aparte aanroep, zoekt beeld per tekstblok en rendert. Zijn er minder dan 15 ongebruikte video's voor een opening, dan haalt `make` eerst zelf nieuw beeld op (ingest, scenes, embed; 2 per zoekterm). Uitzetten: `--no-restock`. |
| 6 | `python -m shorts review` | Open http://127.0.0.1:5000: video, script, elk feit met bron (twijfelgevallen oranje/rood), titel aanpassen, goedkeuren of afwijzen met reden. |
| 7 | `python -m shorts upload` | Uploadt goedgekeurde Shorts als privé met een publicatietijd (standaard 15:00 en 21:00 UTC, max 2 per dag). |
| 8 | `python -m shorts stats` | Haalt views, engaged views en kijkpercentage op; na ~10 Shorts met cijfers stuurt dit de hero-shotkeuze bij. |
| – | `python -m shorts status` | Telling per stap. |
| – | `python -m shorts cleanup` | Verwijdert video's die op zijn: alle bruikbare scènes gebruikt en die Shorts geüpload of afgewezen (plus dubbele video's). Eerst kijken: `--dry-run`. Een verwijderde video wordt nooit opnieuw gedownload. |
| – | `python -m shorts finish` | Maakt Shorts af die halverwege stopten (bijv. Claude-limiet bereikt). |

**Muziek (optioneel):** elk kanaal heeft een eigen map: `C:\shorts-data\music\<kanaal>`, voor het ruimtekanaal dus `music\space`. Zet daar mp3's uit de YouTube Audio Library in (filter "Attribution not required"). Zonder tracks is de Short stil. Daarna `python -m shorts rerender` om wachtende Shorts opnieuw te renderen.

## Uploaden instellen (fase 2)

1. Ga naar console.cloud.google.com, maak een project, zet **YouTube Data API v3** en **YouTube Analytics API** aan.
2. OAuth-toestemmingsscherm: type "External", voeg jezelf toe als testgebruiker.
3. Credentials → OAuth client ID → **Desktop app**. Download het JSON-bestand als `C:\shorts-data\secrets\client_secret.json`.
4. Eerste `python -m shorts upload --limit 1` opent een browser: log in met het account van het kanaal.

Sleutels en tokens staan alleen in `C:\shorts-data\secrets`, nooit in deze repo.

## Een tweede kanaal

Kopieer `config/channels/space.toml` naar bijv. `config/channels/oceans.toml`, pas niche, zoektermen, formats en stijl aan, en gebruik `--channel oceans`. Elk kanaal krijgt een eigen login-token. Zie het plan (sectie 7) voor wanneer dat zinvol is.

## Code

| Bestand | Stap |
| :-- | :-- |
| `shorts/ingest_nasa.py` | NASA Image and Video Library downloaden, rechtencheck, geluid eraf |
| `shorts/scenes.py` | Scènedetectie, scores (beweging, contrast, titelkaart), uitsnede |
| `shorts/embed.py` | CLIP-embeddings en zoeken |
| `shorts/hero.py` | Hero shot kiezen (+ terugkoppeling uit statistieken) |
| `shorts/script.py`, `shorts/llm.py` | Scripts via `claude -p` |
| `shorts/factcheck.py` | Aparte feitencheck |
| `shorts/shots.py` | Beeld per tekstblok |
| `shorts/render.py` | ffmpeg-montage, ondertitels (ASS), metadata met bronvermelding |
| `shorts/review.py` | Reviewpagina (Flask) |
| `shorts/upload.py` | YouTube-upload, inplannen, statistieken |
| `shorts/library.py` | Footage verhuizen, automatisch bijvullen, opruimen |

Tests: `python -m pytest` (draait een volledige proefrender op nepbeeld, zonder GPU of Claude).
