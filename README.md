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
| `run_goal(goal, texts, app)` | Jev (szybki model decyzyjny przez OpenRouter) sam przeklikuje krótki cel, ok. 1 s na krok |

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

W samym Cursorze agent też nie klika (`CU_HOST_APPS`): to czat, w którym działa, a kliknięcie mogłoby nacisnąć jego przyciski. Pisanie w Cursorze, terminalach i menedżerach haseł jest zablokowane (`CU_PROTECTED_APPS`), a linki `tel:`, `facetime:`, `sms:`, `file:`, `shortcuts:` i podobne nie są otwierane.

### Jev: szybkie przeklikiwanie (`run_goal`)

Najwięcej czasu zajmuje myślenie modelu przed każdym kliknięciem (u Groka ok. 7-8 s na turę). `run_goal`
oddaje krótki cel, np. „otwórz Prywatność i włącz blokowanie wszystkich ciasteczek”, modelowi
[Jev](https://openrouter.ai/typesafe/jev-1.13) firmy TypeSafe. To model decyzyjny, nie czat: dostaje cel,
widoczny tekst okna i listę możliwych akcji (klikalne elementy z Accessibility, wpisanie tekstów podanych przez
Groka, Return, Escape, przewijanie), a zwraca wybraną akcję z prawdopodobieństwem. Serwer wykonuje ją tymi
samymi funkcjami co zwykłe narzędzia (te same okienka potwierdzeń, blokady i zapis do przepisów) i pyta znowu.
Pętla kończy się, gdy Jev uzna cel za osiągnięty, gdy jest niepewny (`CU_JEV_MIN_PROB`), gdy dwie akcje z rzędu
nic nie zmieniły albo po `max_steps`. Grok dostaje log i zrzut ekranu i prowadzi dalej. Tak samo działa
Jev-Mobile (arXiv 2609.30186): model wizyjny wyznacza cele, a drzewo Accessibility wyznacza możliwe akcje.

Jev nie pisze tekstu, więc tekst do wpisania podaje Grok w `texts`. Nie nadaje się do ikon bez etykiet
i do decyzji, których cel nie określa. W aplikacjach chronionych (`CU_PROTECTED_APPS`, np. Cursor) `run_goal`
nie działa wcale.

**Włączenie:** utwórz klucz na [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) i doładuj
konto (Jev nie ma darmowego progu). Sam dopisz klucz w `~/.cursor/mcp.json`, w sekcji `env` serwera
`computer-use` (nie wklejaj go do czatu):

```json
"computer-use": {
  "type": "stdio",
  "command": ".../cursor-computer-use/.venv/bin/python",
  "args": ["-m", "cu_mcp"],
  "env": { "OPENROUTER_API_KEY": "sk-or-..." }
}
```

Potem zamknij Cursora (cmd+Q) i otwórz go ponownie. `.venv/bin/cursor-computer-use --check-jev` robi
3 próbne decyzje (razem poniżej 0,1 grosza) i pokazuje opóźnienie. `install.sh` przy ponownej instalacji
zachowuje sekcję `env`.

**Koszt i prywatność:** płacisz tylko za wejście, ok. $0.042 za 1M tokenów. Jeden krok to zwykle 1-3 tys.
tokenów, czyli ułamek grosza. Do OpenRoutera i TypeSafe trafia cel, tekst widoczny w oknie aplikacji z przodu
i nazwy jej przycisków, ale nie zrzut ekranu. Nie używaj `run_goal` na oknach z danymi, których nie chcesz
wysyłać.

### Efekty na ekranie

Kiedy Grok steruje, na ekranie pojawia się świecąca fioletowa ramka (biegnie krawędzią ekranu i obrysem
notcha; zaokrąglone rogi panelu same ją przycinają), napis pod paskiem menu i fala przy każdym kliknięciu.
Aureola z podpisem „Grok” pojawia się przy kursorze tylko wtedy, gdy ruszał nim Grok, i gaśnie ok. 1 s
później, więc nie chodzi za Twoją myszą. Kursor przesuwa się płynnie (do 0,2 s), a prawdziwa mysz nie
jest blokowana po zdarzeniach Groka. Ramka gaśnie od razu, gdy sam ruszysz myszą (przejmujesz sterowanie) albo zjedziesz kursorem w lewy górny róg, a najpóźniej po 15 s bez akcji.

Nakładka to osobny proces (`overlay.py`) z oknem, które przepuszcza kliknięcia, nie zabiera fokusu
i jest wyłączone z przechwytywania ekranu, więc Grok nie widzi jej na zrzutach.

### Praca w tle (przycisk „Work in background”)

Kiedy Grok pracuje, w prawym dolnym rogu, obok Docka, jest przycisk **Work in background**. Po
kliknięciu serwer tworzy niewidoczny, wirtualny ekran („nowy pulpit”) i przenosi na niego okna aplikacji,
w której pracuje Grok. Grok działa dalej na tamtym ekranie, a Ty normalnie używasz Maca: kursor i
klawiatura są Twoje, ramka gaśnie. Przycisk zmienia się na **Working in background · Show** i jego
kliknięcie odstawia okna dokładnie tam, gdzie były.

Jak to działa:
- macOS nie ma publicznego API do tworzenia pulpitów (Spaces) ani do przenoszenia cudzych okien między
  nimi, więc „pulpit” to wirtualny monitor (prywatna klasa `CGVirtualDisplay`, tej samej używają DeskPad
  i BetterDisplay). Stoi po skosie od prawego dolnego rogu Twojego ekranu i dotyka go w jednym punkcie,
  więc kursor praktycznie na niego nie trafia. Okna na nim normalnie się rysują, a znikają razem z serwerem;
  macOS sam przenosi je wtedy z powrotem.
- W tle Grok nie używa myszy ani klawiatury: klika przez Accessibility (AXPress), pisze prosto do pola
  (AXSelectedText / AXValue, z odczytem kontrolnym), skróty wykonuje przez menu aplikacji, a Return/Escape
  przez AXConfirm/AXCancel. Gdy aplikacja tego nie obsługuje, zdarzenie idzie tylko do niej
  (`CGEventPostToPid`), bez ruszania kursora.
- Okna, które aplikacja otworzy w trakcie, trafiają na ukryty ekran, a jeśli aplikacja ukradnie fokus,
  serwer oddaje go Twojej aplikacji.
- Nie działają: przeciąganie i najeżdżanie myszą. Grok dostaje wtedy komunikat, żeby poprosić Cię o
  **Show**.
- Przycisk nie pojawia się na zrzutach i serwer nie pozwoli Grokowi go kliknąć.

Sprawdzenie na Twoim Macu (w terminalu Cursora, bo to Cursor ma uprawnienia): przenosi świeży dokument
TextEdit na ukryty ekran, wpisuje do niego tekst bez klawiatury i sprawdza OCR-em, że tekst tam jest.
Z nazwą aplikacji testuje ją zamiast TextEdit (wpisuje i czyści tekst w jej pierwszym polu):

```bash
.venv/bin/cursor-computer-use --check-background
```

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
| `CU_SETTLE_QUIET` | `0.3` | jak długo ekran musi stać bez zmian, żeby uznać go za gotowy |
| `CU_SETTLE_LATE` | `2.5` | po naciśnięciu przez Accessibility: ile s czekać na spóźniony efekt (aplikacje webowe) |
| `CU_SETTLE_SEARCH` | `1.2` | po wpisaniu w pole wyszukiwania: ile s czekać na wyniki |
| `CU_PROTECTED_APPS` | `Cursor,Terminal,iTerm2,Ghostty,Warp,Passwords,Keychain Access` | aplikacje, w których pisanie jest zablokowane |
| `CU_HOST_APPS` | `Cursor` | czat, w którym działa agent: nie klika, nie przewija i nie wypełnia w nim nic |
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
| `OPENROUTER_API_KEY` | brak | klucz OpenRouter dla `run_goal` (bez niego narzędzie jest wyłączone) |
| `CU_JEV_MODEL` | `typesafe/jev-1.13` | model Jev (`~typesafe/jev-latest` = zawsze najnowszy) |
| `CU_JEV_MIN_PROB` | `0.5` | poniżej tego prawdopodobieństwa Jev oddaje sterowanie Grokowi |
| `CU_JEV_TIMEOUT` | `10` | limit czasu jednej decyzji w s |
| `CU_BACKGROUND_LABEL` | `Work in background` | napis na przycisku |
| `CU_FOREGROUND_LABEL` | `Working in background · Show` | napis na przycisku w trybie w tle |
| `CU_BUTTON_GRACE` | `4` | ile s przycisk zostaje po tym, jak ruszysz myszą (dłużej, gdy kursor jest przy nim) |
| `CU_BACKGROUND_LATE` | `1.0` | w tle: ile s czekać na spóźniony efekt naciśnięcia |

## Ograniczenia

- Tylko główny monitor (plus ukryty ekran trybu w tle).
- Tryb w tle korzysta z prywatnego API macOS (`CGVirtualDisplay`); jeśli przyszła wersja systemu je zmieni,
  przycisk pokaże błąd, a zwykła praca dalej działa. Część aplikacji ignoruje zdarzenia, gdy nie są
  aktywne: wtedy Grok poprosi o **Show**.
- Skróty klawiszowe używają pozycji klawiszy z układu US (pisanie tekstu działa w każdym układzie).
- Jakość `marks` zależy od aplikacji: natywne aplikacje macOS są świetne, Electron i strony WWW
  czasem potrzebują drugiego wywołania, gry i aplikacje rysujące same siebie nie mają drzewa.
- Uprawnienia macOS dostaje Cursor, nie Python, bo to Cursor uruchamia serwer.

## Testy

```bash
.venv/bin/python -m pytest -q tests
```
