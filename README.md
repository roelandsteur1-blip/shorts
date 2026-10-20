# Shorts-pipeline

Maakt YouTube Shorts over ruimtevaart en sterrenkunde uit rechtenvrij NASA-beeld, met alleen tekst op beeld. Jij keurt elke Short goed; daarna plant de pipeline hem in op YouTube.

```
ingest ─► scenes ─► embed ─► make (hero shot ─► script ─► feitencheck ─► beeld per tekstblok ─► render) ─► review ─► upload ─► stats
```

## Installeren

Alles draait op een Linux-server (Ubuntu 24.04); het dashboard bereik je via Tailscale. Zie [deploy/README.md](deploy/README.md): `bash setup.sh` installeert Python, ffmpeg, PyTorch (CPU), de pakketten, het lettertype, de systemd-dienst `shorts-dashboard` en de nachtelijke backup, en draait `shorts check`.

Database, renders en footage staan in `~/shorts-data` (aan te passen met `data_dir` / `footage_dir` in `config/local.toml`). Downloaden stopt als er minder dan `min_free_gb` (standaard 20 GB) vrij is.

## Dagelijks gebruik

Op de server, in `~/shorts`: `.venv/bin/python -m shorts <commando>`. Bijna alles kan ook met knoppen op het dashboard.

| Stap | Commando | Wat het doet |
| :-- | :-- | :-- |
| 1 | `python -m shorts ingest --per-term 5` | Downloadt NASA-video's voor de zoektermen in `config/channels/space.toml`, haalt het geluid eraf. Video's met een credit van derden (SpaceX, ESA, "courtesy"…) worden overgeslagen. |
| 1 | `python -m shorts scenes` | Knipt in scènes, gooit zwart/titelkaarten weg, bepaalt waar de 9:16-uitsnede moet. |
| 1 | `python -m shorts embed` | Maakt scènes doorzoekbaar met CLIP (geen Claude-gebruik). |
| 2–5 | `python -m shorts make -n 5` | Kiest 5 hero shots, laat Claude er scripts bij schrijven (1 aanroep per 5), controleert de feiten in een aparte aanroep, zoekt beeld per tekstblok en rendert. De stappen lopen door elkaar: terwijl Claude de volgende 5 schrijft, wordt de vorige 5 gecontroleerd en gerenderd. Alleen als er te weinig beeld is voor deze batch haalt `make` eerst nieuw beeld op; het bijvullen tot 15 gebeurt ná het renderen, als de Shorts al klaarstaan voor review. Uitzetten: `--no-restock`. |
| 2–5 | `python -m shorts make-all --to 10` | Vult elk kanaal aan tot 10 wachtende Shorts, in één run (de knop *Make for every channel*): Claude schrijft het volgende kanaal terwijl het vorige rendert. Beeld voor de volgende aanvulling wordt al gedownload terwijl Claude schrijft. |
| 6 | `python -m shorts dashboard` | Het dashboard op http://127.0.0.1:5000; draait als dienst, bereikbaar via Tailscale. Zie [Dashboard](#dashboard). |
| 7 | `python -m shorts upload` | Uploadt goedgekeurde Shorts als privé met een publicatietijd (standaard 15:00 en 21:00 UTC, max 2 per dag). |
| 8 | `python -m shorts stats` | Haalt views, engaged views en kijkpercentage op; na ~10 Shorts met cijfers stuurt dit de hero-shotkeuze bij. |
| – | `python -m shorts status` | Telling per stap. |
| – | `python -m shorts cleanup` | Verwijdert video's die op zijn: alle bruikbare scènes gebruikt of het maximum aantal Shorts eruit gehaald, en die Shorts geüpload of afgewezen (plus dubbele video's). Eerst kijken: `--dry-run`. Een verwijderde video wordt nooit opnieuw gedownload. |
| – | `python -m shorts finish` | Maakt Shorts af die halverwege stopten (bijv. Claude-limiet bereikt). |
| – | `python -m shorts check` | Controleert Python, ffmpeg, PyTorch, Claude Code en de schijfruimte. |
| – | `python -m shorts backup` | Kopie van de database (draait elke nacht via cron, 7 bewaard). |

**Achtergrond:** zware taken draaien op lage prioriteit, dus het dashboard blijft vlot. Hoe lang elke stap duurt en hoeveel Claude je de laatste 5 uur gebruikte staat onderaan **Stats**. Haalt Claude de limiet van je abonnement, dan stopt alleen het schrijven: wat al geschreven is wordt nog gecontroleerd en gerenderd, de rest gaat verder bij de volgende run.

**Muziek:** gaat vanzelf. 'make' haalt CC0/publiek-domein muziek van Openverse (en Freesound zodra je op de Music-pagina een gratis key invult) zodra de bibliotheek minder dan `music_min_tracks` (30) tracks heeft, met de zoekwoorden uit `music_terms` van alle kanalen. CLAP (lokaal) geeft elke track een stemming en gooit ruis, spraak en geluidseffecten weg; elke Short krijgt de minst gebruikte track in de stemming van het script, vanaf waar de muziek echt begint. Alle kanalen delen één muziekbibliotheek (`<data_dir>/music`); de stemming bepaalt welke Short welke track krijgt. Eigen mp3's (YouTube Audio Library, "Attribution not required") kun je daar ook in zetten. Op de Music-pagina: luisteren, stemming aanpassen, en **Claimed** als YouTube een track claimt (nooit meer gebruikt, Shorts die nog niet geüpload zijn worden opnieuw gerenderd). Met de hand: `python -m shorts music`.

## Dashboard

Het dashboard draait als dienst op de server en luistert alleen op 127.0.0.1; via Tailscale (`tailscale serve`) open je het vanaf je eigen apparaten. Er staat niets open naar het internet.

- **Home**: per kanaal een kaart met Shorts om te reviewen, goedgekeurd, ingepland voor de komende 7 dagen, aantal ongebruikte video's en de laatste taak. Knoppen *Make Shorts*, *Review*, *Schedule*.
- **Make**: kies kanaal, aantal en format en start. Het werk draait op de achtergrond als `python -m shorts --channel <id> make -n N`, met de huidige stap (download / script / feitencheck / render), "3 van 10", de laatste logregels en een Stop-knop. Er draait één zware taak tegelijk; de rest wacht in de rij. Reviewen kan intussen gewoon.
- **Review**: dezelfde reviewpagina als eerst, met een kanaalfilter. Toetsen: <kbd>A</kbd> goedkeuren, <kbd>R</kbd> afwijzen (met de gekozen reden), <kbd>N</kbd> volgende, <kbd>E</kbd> tekst bewerken, spatie pauze; de volgende Short laadt al op de achtergrond. Een fout woord? Pas de tekst van een blok aan en klik *Save text and re-render*: de Short wordt opnieuw gerenderd met hetzelfde beeld (de feitencheck draait niet opnieuw).
- **Schedule**: per kanaal de goedgekeurde Shorts met de tijd waarop ze zouden verschijnen, de knop *Schedule approved now* (uploadt ze privé met een publicatietijd), wat er per dag ingepland staat en wat al online is. Bovenaan het aantal uploads vandaag van de gedeelde limiet (`upload_quota` in `config/settings.toml`, standaard 100; reset om middernacht Californische tijd). `upload` stopt vanzelf bij die limiet.
- **Stats**: views, engaged views (het dichtst bij "bekeken vs weggeswipet") en kijkpercentage per Short, per kanaal en per format. *Fetch stats* haalt nieuwe cijfers op.
- **Library**: per kanaal welke video's het gebruikt (Claude sorteert elke video tegen de niche van elk kanaal; een verkeerde verbeter je met één klik), en per categorie het aantal bruikbare scènes. Categorieën bepalen alleen waar nieuw beeld gezocht wordt.
- **Sources**: elke bron met aan/uit, licentie, gedownload en afgewezen (met de redenen), en een proefzoektocht.
- **Jobs**: alle taken met hun log. Is het dashboard midden in een taak herstart, dan staat die op *interrupted*: een `make` krijgt de knop *Finish* (draait `finish`), andere taken *Run again*.

Logbestanden van taken staan in `~/shorts-data/logs`.

## Beeldbronnen en categorieën

Naast NASA kan de pipeline beeld halen uit **Internet Archive**, **Prelinger Archives** en **Wikimedia Commons** (geen API-sleutel nodig). Elke bron heeft een eigen module in `shorts/sources/` die zoekt, de licentie per video beoordeelt en het bestand aanwijst. Alleen publiek domein, CC0 en CC BY worden gebruikt; CC BY-SA, niet-commerciële licenties en video's zonder herkenbare licentie worden afgewezen (en geteld op de Sources-pagina). Voor CC BY komt de verplichte naamsvermelding vanzelf in de beschrijving, zonder links.

Gemengde archieven worden per **scène** in categorieën gesorteerd (`config/categories.toml`: ruimte, oorlog, geschiedenis, luchtvaart, natuur, oceaan, wetenschap, steden, vervoer, sport, dagelijks leven, rampen): eerst op de eigen metadata van het archief, dan met CLIP, en alleen als die het oneens zijn of allebei twijfelen met één Claude-aanroep per 50 video's.

Met een gratis API-sleutel (in te vullen op de Sources-pagina) komen daar **Europeana** en **Pixabay** bij. Pixabay is stock-beeld: wel tussen de tekstblokken, nooit als hero shot. Video's die volgens de bron te lang of te groot zijn worden niet eerst gedownload; van 4K-video's op Commons wordt de 1080p-versie gehaald.

Per kanaal in de TOML: `categories = ["space"]` en `sources = ["nasa"]`. Hero shots en beeld per tekstblok komen alleen uit scènes in de categorieën van het kanaal; `make` vult de categorieën bij die bijna op zijn, met de zoekhints van elke categorie per bron.

| Commando | Wat het doet |
| :-- | :-- |
| `python -m shorts sources` | Lijst van bronnen: aan/uit, licentie, gedownload, afgewezen. |
| `python -m shorts sources --try prelinger "rocket"` | Proefzoektocht zonder downloaden: per video de licentiebeslissing. |
| `python -m shorts ingest --source prelinger --term "subject:(aviation)"` | Downloaden van één bron. |
| `python -m shorts restock` | Bijvullen van de categorieën van het kanaal die bijna op zijn. |
| `python -m shorts status --per-video` | Per video hoeveel Shorts er nog uit kunnen. |
| `python -m shorts categorize` | Nieuwe scènes sorteren (`--all`: alles opnieuw, na het aanpassen van `config/categories.toml`). |

**Lange video's geven meer Shorts.** Elke 90 seconden video is goed voor één Short, met een maximum van 5 per video (`segment_seconds` en `max_shorts_per_video` in `config/settings.toml`). De hero shots uit één video liggen minstens 90 seconden uit elkaar en mogen niet op elkaar lijken. Script en feitencheck krijgen te zien wat eerdere Shorts uit die video al vertelden, en bij inplannen zitten er minstens 2 dagen tussen Shorts uit dezelfde video (`sibling_gap_days`).

## Uploaden instellen (fase 2)

1. Ga naar console.cloud.google.com, maak een project, zet **YouTube Data API v3** en **YouTube Analytics API** aan.
2. OAuth-toestemmingsscherm: type "External", voeg jezelf toe als testgebruiker.
3. Credentials → OAuth client ID → **Desktop app**. Zet het JSON-bestand op de server als `~/shorts-data/secrets/client_secret.json`.
4. Per kanaal **Connect YouTube** op de kanaalpagina, met de ssh-tunnel uit [deploy/README.md](deploy/README.md): log in met het account van het kanaal.

Sleutels en tokens staan alleen in `~/shorts-data/secrets`, nooit in deze repo.

## Een tweede kanaal

Op het dashboard: **Add channel**. Een nieuw YouTube-kanaal maak je eerst zelf aan (de API kan dat niet; kies een eigen naam, dan wordt het een Brand Account). Daarna: naam, taal en niche invullen, *Draft with Claude* vult niche-tags, categorieën, bronnen, zoektermen en formats voor, jij past aan en slaat op. Op de kanaalpagina staan dan de stappen: **Connect YouTube** (het dashboard laat zien welk kanaal de login vond en jij bevestigt het) en **Try it** (`make -n 3`).

Uploaden weigert als de login bij een ander kanaal hoort dan het bevestigde (handig met meerdere Brand Accounts onder één Google-account). Alle kanalen delen één Google Cloud-project en dus de uploadlimiet per dag.

Instellingen wijzigen kan op dezelfde pagina; de oude TOML blijft bewaard als `.bak`. Met de hand bewerken van `config/channels/<id>.toml` werkt ook. Vanaf de commandoregel: `python -m shorts --channel <id> connect`.

## Code

| Bestand | Stap |
| :-- | :-- |
| `shorts/sources/` | Beeldbronnen (NASA, Internet Archive, Prelinger, Commons, Europeana, Pixabay); `common.py` doet opslaan, rechtencheck, downloaden, geluid eraf, dubbele overslaan |
| `shorts/categorize.py`, `config/categories.toml` | Scènes in categorieën sorteren (metadata, CLIP, Claude) |
| `shorts/scenes.py` | Scènedetectie in één ffmpeg-pass (lange video's verdeeld over meerdere workers), scores (beweging, contrast, titelkaart), uitsnede |
| `shorts/speed.py`, `shorts/timing.py` | Hoeveel er tegelijk draait, lage prioriteit, tijden per stap |
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
| `shorts/library.py` | Footage automatisch bijvullen, opruimen |

Tests: `python -m pytest` (draait een volledige proefrender op nepbeeld, zonder Claude).
