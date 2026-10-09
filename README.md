# cursor-computer-use

Komenda `/grok-computer-use` w Cursorze, która pozwala modelowi obsługiwać Maca jak człowiek: widzi ekran,
klika, pisze, przewija. Działa tak jak computer use u Claude'a: pętla „zrzut ekranu → jedna akcja → nowy zrzut”,
wzbogacona o drzewo Accessibility i OCR.

**Modele:** serwer MCP nie zależy od modelu. Powinien działać z każdym modelem w Cursorze, który przyjmuje obrazy
i wywołuje narzędzia (Grok, Claude, GPT, Gemini). Testowane z Grokiem 4.7, stąd nazwa komendy i domyślne
napisy „Grok” na ekranie (zmienisz je przez `CU_OVERLAY_LABEL` i `CU_CURSOR_LABEL`). Według badań w `research/`
narzędzia pomagają modelom rozumującym, a słabszym modelom bez rozumowania potrafią zaszkodzić.

Dwie części:

- **Serwer MCP** (`src/cu_mcp/`, Python): robi zrzuty ekranu i wysyła mysz/klawiaturę
  przez Quartz, czyta drzewo Accessibility macOS.
- **Skill** (`skill/grok-computer-use/SKILL.md`): instrukcja obsługi dla modelu (pętla pracy,
  układ współrzędnych, kiedy czego używać, zasady bezpieczeństwa). Cursor pokazuje go jako `/grok-computer-use`.

## Instalacja

```bash
./install.sh
```

Skrypt tworzy `.venv`, kopiuje skill do `~/.cursor/skills/grok-computer-use/` i dopisuje serwer
`computer-use` do `~/.cursor/mcp.json` (stary plik zostaje jako `mcp.json.bak`).

Potem:

1. System Settings → Privacy & Security → **Accessibility** (w macOS 27 nazywa się **Device Control and Data Access**): włącz Cursor.app.
2. Tamże → **Screen & System Audio Recording**: włącz Cursor.
3. W Cursorze: Customize → MCPs → computer-use → **Reload** (albo restart Cursora).
4. W terminalu Cursora: `.venv/bin/cursor-computer-use --check` (nic nie klika, tylko sprawdza
   uprawnienia, robi zrzut i czyta elementy aktywnej aplikacji).
5. Żeby agent nie pytał o zgodę przy każdym kliknięciu, dodaj narzędzia serwera
   `computer-use` do allowlisty w ustawieniach Cursora (Agents → auto-run). Bez tego każda akcja
   czeka na Twoje „Run”.

## Użycie

W czacie Agenta wybierz model z obsługą obrazów (np. Grok 4.7) i napisz:

```
/grok-computer-use otwórz Notatki i utwórz notatkę "zakupy" z listą: mleko, chleb
```

**Awaryjne zatrzymanie:** przesuń mysz w lewy górny róg ekranu. Serwer od razu odrzuca
kolejne akcje (komunikat ABORTED) aż do restartu serwera MCP (wyłącz i włącz go w ustawieniach
MCP Cursora). Możesz też po prostu kliknąć Stop w czacie.

## Narzędzia

| Narzędzie | Co robi |
|---|---|
| `screenshot(marks, grid)` | zrzut głównego ekranu; `marks` rysuje ponumerowane ramki na klikalnych elementach, `grid` siatkę co 100 px |
| `read_text(region?)` | OCR: tekst z ekranu z pozycjami, tańszy od obrazka |
| `find(text)` | wszystko, co pasuje do tekstu (Accessibility + OCR), bez klikania |
| `click_text("Options")` | klik w cokolwiek z widocznym tekstem: najpierw Accessibility, potem OCR; `near="..."` wybiera dopasowanie najbliżej innego tekstu |
| `fill(text, target, submit)` | pole tekstowe w jednym wywołaniu: znajdź, wyczyść, wpisz, Enter |
| `click`, `click_element` | klik w punkt (lewy/prawy/środkowy, 1-3 razy, z modyfikatorami) albo w element o numerze |
| `move_mouse`, `drag`, `scroll` | najechanie, przeciągnięcie, przewijanie |
| `type_text`, `key` | pisanie dowolnego Unicode (też polskie znaki), skróty typu `cmd+shift+t` |
| `menu("File > Export > PDF")` | wybór z paska menu przez Accessibility; `menu("View")` wypisuje zawartość menu |
| `open_app`, `open_url` | uruchamianie aplikacji, linki i deep linki (`spotify:search:...`) |
| `wait_for(text)` | czeka, aż tekst się pojawi (albo zniknie), zamiast zgadywać czas |
| `scroll_to(text)` | przewija, aż tekst będzie widoczny; mówi, gdy lista się skończy |
| `list_apps`, `ui_elements` | lista okien, lista elementów jako tekst |
| `zoom` | powiększenie fragmentu z siatką we współrzędnych pełnego zrzutu (można od razu klikać) |
| `wait`, `clipboard`, `batch` | czekanie, schowek, kilka akcji z jednym zrzutem na końcu |
| `app_action(app, action, args)` | gotowe akcje AppleScript: Muzyka/Spotify (play, pause, utwór), Notatki, Przypomnienia, Safari, Finder, szkic maila, głośność |
| `recipe_save`, `recipe_run`, `recipe_list` | zapis udanego przebiegu z parametrami i odtworzenie go jednym wywołaniem |

Każda akcja czeka, aż ekran przestanie się zmieniać, i zwraca nowy zrzut oraz informację, gdzie jest fokus klawiatury. Jeśli nic się nie zmieniło, zamiast obrazka wraca „No visible change”, co jest sygnałem dla modelu i oszczędza tokeny.

Każde kliknięcie raportuje, w co naprawdę trafiło (element pod punktem z Accessibility). Jeśli cel
przykrył pop-up albo inne okno, serwer naciska właściwy element przez Accessibility, zamiast klikać w to, co go zasłania.

Kliknięcia i polecenia menu typu Wyślij, Kup, Zapłać, Usuń, Opublikuj (lista: `CU_CONFIRM_WORDS`) zatrzymuje
sam serwer i pyta Cię systemowym okienkiem. Model nie może go kliknąć, bo czeka na nie wywołanie trzymające
serwer, a brak odpowiedzi przez 60 s oznacza „nie”. Badania pokazują, że samo proszenie modelu o ostrożność
nie chroni przed pop-upami i wstrzykniętymi poleceniami (szczegóły: `research/draft.final.html`).

Przepisy (`~/Library/Application Support/cursor-computer-use/recipes/`) to zapisane sekwencje udanych akcji
z parametrami, np. `spotify-like-song(song)`. Pierwszy zrzut zadania pokazuje ich listę, a `recipe_run`
odtwarza całość jednym wywołaniem i oddaje sterowanie Grokowi przy pierwszym kroku, który nie pasuje do ekranu.

Pisanie w Cursorze, terminalach i menedżerach haseł jest zablokowane (`CU_PROTECTED_APPS`), a linki `tel:`, `facetime:`, `sms:`, `file:`, `shortcuts:` i podobne nie są otwierane.

### Efekty na ekranie

Kiedy Grok steruje, na ekranie pojawia się świecąca fioletowa ramka (biegnie krawędzią ekranu i obrysem
notcha; zaokrąglone rogi panelu same ją przycinają), napis pod paskiem menu i fala przy każdym kliknięciu.
Aureola z podpisem „Grok” pojawia się przy kursorze tylko wtedy, gdy ruszał nim Grok, i gaśnie ok. 1 s
później, więc nie chodzi za Twoją myszą. Kursor przesuwa się płynnie (do 0,2 s), a prawdziwa mysz nie
jest blokowana po zdarzeniach Groka. Ramka gaśnie od razu, gdy sam ruszysz myszą (przejmujesz sterowanie) albo zjedziesz kursorem w lewy górny róg, a najpóźniej po 15 s bez akcji.

Nakładka to osobny proces (`overlay.py`) z oknem, które przepuszcza kliknięcia, nie zabiera fokusu
i jest wyłączone z przechwytywania ekranu, więc Grok nie widzi jej na zrzutach.

### Dlaczego Grok trafia w przyciski

Claude jest specjalnie trenowany do wskazywania pikseli na zrzutach. Inne modele bywają mniej
precyzyjne, więc serwer daje im podpórki:

- zrzut jest zmniejszany do 1280 px dłuższego boku, a współrzędne modelu są przeliczane na
  punkty ekranu (Retina i skalowanie obsłużone po stronie serwera);
- `marks=true` (set-of-marks): ramki z numerami z drzewa Accessibility, klik po numerze
  zamiast zgadywania pikseli; pozycja elementu jest odczytywana na nowo tuż przed kliknięciem;
- `menu()` i skróty klawiszowe omijają klikanie w ogóle.

## Konfiguracja (zmienne środowiskowe w `mcp.json` → `env`)

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CU_MAX_EDGE` | `1280` | dłuższy bok zrzutu w px |
| `CU_IMAGE_FORMAT` | `jpeg` | `jpeg` albo `png` |
| `CU_JPEG_QUALITY` | `80` | jakość JPEG |
| `CU_SETTLE` | `0.12` | minimalne czekanie po akcji |
| `CU_SETTLE_MAX` | `1.5` | maksymalne czekanie, aż ekran przestanie się zmieniać |
| `CU_PROTECTED_APPS` | `Cursor,Terminal,iTerm2,Ghostty,Warp,Passwords,Keychain Access` | aplikacje, w których pisanie jest zablokowane |
| `CU_OCR_LANGUAGES` | `pl-PL,en-US` | języki OCR |
| `CU_FAST_CAPTURE` | `1` | `0` = zrzuty przez `screencapture` zamiast w procesie |
| `CU_GRID` | `0` | `1` = siatka na każdym zrzucie |
| `CU_AUTO_SCREENSHOT` | `1` | `0` = akcje nie zwracają zrzutu |
| `CU_EFFECTS` | `1` | `0` = bez ramki, aureoli i fal |
| `CU_OVERLAY_COLOR` | `#8B5CF6` | kolor ramki i aureoli |
| `CU_OVERLAY_LABEL` | `Grok steruje komputerem · …` | napis pod paskiem menu |
| `CU_CURSOR_LABEL` | `Grok` | podpis przy kursorze |
| `CU_OVERLAY_IDLE` | `15` | po ilu sekundach bez akcji efekty gasną (od razu, gdy ruszysz myszą) |
| `CU_GLIDE` | `0.2` | czas płynnego ruchu kursora w s; `0` = skok |
| `CU_CONFIRM` | `1` | `0` = bez okienek potwierdzenia (niezalecane) |
| `CU_CONFIRM_WORDS` | wyślij, kup, zapłać, usuń… | słowa wymagające potwierdzenia (po przecinku) |
| `CU_RECIPES_DIR` | `~/Library/Application Support/cursor-computer-use/recipes` | gdzie leżą przepisy |

## Ograniczenia

- Tylko główny monitor.
- Skróty klawiszowe używają pozycji klawiszy z układu US (pisanie tekstu działa w każdym układzie).
- Jakość `marks` zależy od aplikacji: natywne aplikacje macOS są świetne, Electron i strony WWW
  czasem potrzebują drugiego wywołania, gry i aplikacje rysujące same siebie nie mają drzewa.
- Uprawnienia macOS dostaje Cursor, nie Python, bo to Cursor uruchamia serwer.

## Testy

```bash
.venv/bin/python -m pytest -q tests
```
