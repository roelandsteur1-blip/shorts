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
| 6 | `python -m shorts dashboard` | Open http://127.0.0.1:5000 (alleen op deze laptop). Zie [Dashboard](#dashboard). `review` doet hetzelfde. |
| 7 | `python -m shorts upload` | Uploadt goedgekeurde Shorts als privé met een publicatietijd (standaard 15:00 en 21:00 UTC, max 2 per dag). |
| 8 | `python -m shorts stats` | Haalt views, engaged views en kijkpercentage op; na ~10 Shorts met cijfers stuurt dit de hero-shotkeuze bij. |
| – | `python -m shorts status` | Telling per stap. |
| – | `python -m shorts cleanup` | Verwijdert video's die op zijn: alle bruikbare scènes gebruikt en die Shorts geüpload of afgewezen (plus dubbele video's). Eerst kijken: `--dry-run`. Een verwijderde video wordt nooit opnieuw gedownload. |
| – | `python -m shorts finish` | Maakt Shorts af die halverwege stopten (bijv. Claude-limiet bereikt). |

**Muziek (optioneel):** elk kanaal heeft een eigen map: `C:\shorts-data\music\<kanaal>`, voor het ruimtekanaal dus `music\space`. Zet daar mp3's uit de YouTube Audio Library in (filter "Attribution not required"). Zonder tracks is de Short stil. Daarna `python -m shorts rerender` om wachtende Shorts opnieuw te renderen.

## Dashboard

`python -m shorts dashboard` en open http://127.0.0.1:5000. Het dashboard luistert alleen op de laptop zelf (geen wachtwoord, niet bereikbaar vanaf je telefoon of het netwerk).

- **Home**: per kanaal een kaart met Shorts om te reviewen, goedgekeurd, ingepland voor de komende 7 dagen, aantal ongebruikte video's en de laatste taak. Knoppen *Make Shorts*, *Review*, *Schedule*.
- **Make**: kies kanaal, aantal en format en start. Het werk draait op de achtergrond als `python -m shorts --channel <id> make -n N`, met de huidige stap (download / script / feitencheck / render), "3 van 10", de laatste logregels en een Stop-knop. Er draait één zware taak tegelijk; de rest wacht in de rij. Reviewen kan intussen gewoon.
- **Review**: dezelfde reviewpagina als eerst, met een kanaalfilter.
- **Library**: per categorie het aantal bruikbare scènes en hoeveel video's elk kanaal nog over heeft. Klik een categorie voor de plaatjes; een verkeerde categorie verplaats je met één klik (wordt onthouden en helpt de automatische sortering). *Uncategorised* is de wachtrij om met de hand te sorteren. Nieuwe categorie: naam typen, *Draft with Claude* vult trefwoorden, CLIP-beschrijvingen en zoekhints in.
- **Sources**: elke bron met aan/uit, licentie, gedownload en afgewezen (met de redenen), en een proefzoektocht.
- **Jobs**: alle taken met hun log. Is de laptop midden in een taak herstart, dan staat die op *interrupted*: een `make` krijgt de knop *Finish* (draait `finish`), andere taken *Run again*.

Logbestanden van taken staan in `C:\shorts-data\logs`.

## Beeldbronnen en categorieën

Naast NASA kan de pipeline beeld halen uit **Internet Archive**, **Prelinger Archives**, **Library of Congress** en **Wikimedia Commons** (geen API-sleutel nodig). Elke bron heeft een eigen module in `shorts/sources/` die zoekt, de licentie per video beoordeelt en het bestand aanwijst. Alleen publiek domein, CC0 en CC BY worden gebruikt; CC BY-SA, niet-commerciële licenties en video's zonder herkenbare licentie worden afgewezen (en geteld op de Sources-pagina). Voor CC BY komt de verplichte naamsvermelding vanzelf in de beschrijving, zonder links.

Gemengde archieven worden per **scène** in categorieën gesorteerd (`config/categories.toml`: ruimte, oorlog, geschiedenis, luchtvaart, natuur, oceaan, wetenschap, steden, vervoer, sport, dagelijks leven, rampen): eerst op de eigen metadata van het archief, dan met CLIP op de GPU, en alleen als die het oneens zijn of allebei twijfelen met één Claude-aanroep per 50 video's. Wat daarna nog twijfelt staat bij *Uncategorised* op de Library-pagina, om met één klik te sorteren.

Per kanaal in de TOML: `categories = ["space"]` en `sources = ["nasa"]`. Hero shots en beeld per tekstblok komen alleen uit scènes in de categorieën van het kanaal; `make` vult de categorieën bij die bijna op zijn, met de zoekhints van elke categorie per bron.

| Commando | Wat het doet |
| :-- | :-- |
| `python -m shorts sources` | Lijst van bronnen: aan/uit, licentie, gedownload, afgewezen. |
| `python -m shorts sources --try loc "rocket"` | Proefzoektocht zonder downloaden: per video de licentiebeslissing. |
| `python -m shorts ingest --source prelinger --term "subject:(aviation)"` | Downloaden van één bron. |
| `python -m shorts restock` | Bijvullen van de categorieën van het kanaal die bijna op zijn. |
| `python -m shorts categorize` | Nieuwe scènes sorteren (`--all`: alles opnieuw, handmatige keuzes blijven). |

## Uploaden instellen (fase 2)

1. Ga naar console.cloud.google.com, maak een project, zet **YouTube Data API v3** en **YouTube Analytics API** aan.
2. OAuth-toestemmingsscherm: type "External", voeg jezelf toe als testgebruiker.
3. Credentials → OAuth client ID → **Desktop app**. Download het JSON-bestand als `C:\shorts-data\secrets\client_secret.json`.
4. Eerste `python -m shorts upload --limit 1` opent een browser: log in met het account van het kanaal.

Sleutels en tokens staan alleen in `C:\shorts-data\secrets`, nooit in deze repo.

## Een tweede kanaal

Op het dashboard: **Add channel**. Een nieuw YouTube-kanaal maak je eerst zelf aan (de API kan dat niet; kies een eigen naam, dan wordt het een Brand Account). Daarna: naam, taal en niche invullen, *Draft with Claude* vult hashtags, categorieën, bronnen, zoektermen en formats voor, jij past aan en slaat op. Op de kanaalpagina staan dan de stappen: muziekmap, **Connect YouTube** (opent een browser op de laptop; het dashboard laat zien welk kanaal de login vond en jij bevestigt het) en **Try it** (`make -n 3`).

Uploaden weigert als de login bij een ander kanaal hoort dan het bevestigde (handig met meerdere Brand Accounts onder één Google-account). Alle kanalen delen één Google Cloud-project en dus de uploadlimiet per dag.

Instellingen wijzigen kan op dezelfde pagina; de oude TOML blijft bewaard als `.bak`. Met de hand bewerken van `config/channels/<id>.toml` werkt ook. Vanaf de commandoregel: `python -m shorts --channel <id> connect`.

## Code

| Bestand | Stap |
| :-- | :-- |
| `shorts/sources/` | Beeldbronnen (NASA, Internet Archive, Prelinger, Library of Congress, Commons); `common.py` doet opslaan, rechtencheck, downloaden, geluid eraf, dubbele overslaan |
| `shorts/categorize.py`, `config/categories.toml` | Scènes in categorieën sorteren (metadata, CLIP, Claude) |
| `shorts/scenes.py` | Scènedetectie, scores (beweging, contrast, titelkaart), uitsnede |
| `shorts/embed.py` | CLIP-embeddings en zoeken |
| `shorts/hero.py` | Hero shot kiezen (+ terugkoppeling uit statistieken) |
| `shorts/script.py`, `shorts/llm.py` | Scripts via `claude -p` |
| `shorts/factcheck.py` | Aparte feitencheck |
| `shorts/shots.py` | Beeld per tekstblok |
| `shorts/render.py` | ffmpeg-montage, ondertitels (ASS), metadata met bronvermelding |
| `shorts/web/`, `shorts/templates/` | Dashboard (Flask): home, make, review, jobs, library, sources |
| `shorts/jobs.py` | Achtergrondtaken: één zware taak tegelijk, voortgang uit `PROGRESS`-regels |
| `shorts/upload.py` | YouTube-upload, inplannen, statistieken, kanaalcontrole |
| `shorts/channels.py`, `shorts/tomlw.py` | Kanaal-TOML aanmaken en bewerken vanuit het dashboard, concept via Claude |
| `shorts/library.py` | Footage verhuizen, automatisch bijvullen, opruimen |

Tests: `python -m pytest` (draait een volledige proefrender op nepbeeld, zonder GPU of Claude).
