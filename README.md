# Hub LagunBus

Implementacja huba instalacji [LagunBus](../lagunbus-spec/) w Pythonie.

| Dokument | Zawartość |
|---|---|
| [`ARCHITEKTURA.md`](ARCHITEKTURA.md) | jak hub jest zbudowany w środku |
| [`KONFIGURACJA.md`](KONFIGURACJA.md) | powiązania, źródła danych, ustawienia adapterów |
| [`../devices/`](../devices/) | oprogramowanie węzłów fizycznych (ESP32-C3) — osobne repozytorium |

**Stan: potwierdzony POC; przed nami wyjście na sprzęt** (§ [Raspberry Pi](#raspberry-pi) niżej).
Naciśnięcie klawisza zapala przekaźnik, a dopasowanie powiązania zajmuje kilkadziesiąt
mikrosekund. Hub wystawia własne API — sterowanie, konfigurację, strumień zdarzeń
i ruch na magistrali — a nad nim stoją powłoka i widok pełnoekranowy.

---

## Uruchomienie

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Wymaga Pythona 3.12+. Na Linuksie wszystko działa na prawdziwym SocketCAN;
na macOS dostępna jest magistrala wirtualna, która udaje także arbitraż.

## W PyCharmie

Otwórz jako projekt **katalog `hub/`**, nie katalog nadrzędny — repozytorium jest tutaj
i tutaj leżą konfiguracje uruchomieniowe.

**Raz, przy pierwszym otwarciu:**

1. *Settings → Project → Python Interpreter* → **Add Local Interpreter → Existing** →
   `hub/.venv/bin/python`. Jeśli `.venv` jeszcze nie ma, zrób je jak w [Uruchomieniu](#uruchomienie).

Potem w liście konfiguracji są gotowe pozycje:

| | Co uruchamia |
|---|---|
| **2 · Shell** | `lagun-hub shell` przeciwko chodzącemu hubowi |
| **2b · Full-screen view** | widok pełnoekranowy, też przeciwko chodzącemu hubowi |
| **4 · Identifier registry** | tabela identyfikatorów, jaką hub widzi |
| **5 · Tests** | `pytest` na całym katalogu |

Powłoka i widok **nie startują huba — dołączają się do chodzącego**, więc hub musi już
pracować (`lagun-hub run`). Obie mają włączone *Emulate terminal in output console*,
bo obie czytają z klawiatury; bez tego PyCharm nie przepuści wpisywania, a powłoce nie
zadziała tabulator.

### Co warto zobaczyć

Przed wersją 0.5 służyło do tego `lagun-sim demo`: jeden proces z klawiszem, przekaźnikiem
i hubem. Symulator wyszedł z huba (patrz [Czym hub rozmawia w testach](#czym-hub-rozmawia-w-testach)),
więc **dziś huba ogląda się albo na prawdziwych urządzeniach**
([`../devices/`](../devices/)), albo w testach — te same ścieżki, tylko bez czekania
na zegar:

| Co chcesz zobaczyć | Gdzie |
|---|---|
| całą drogę klawisz → hub → przekaźnik | `tests/test_e2e.py` |
| powtórzenia, deduplikację, ciszę węzła | `tests/test_e2e.py`, `tests/test_transactions.py` |
| jak węzeł bez adresu wchodzi do instalacji | `tests/test_enumeration.py` |
| doprowadzanie węzła do zgodności z plikiem | `tests/test_reconcile.py` |

```sh
.venv/bin/python -m pytest tests/test_e2e.py -v
```

**Najciekawsza rzecz do zobaczenia w IDE.** Przy chodzącym hubie otwórz
`config/nodes.yaml`, zmień `Purpose: LIGHT` na `VALVE` i zapisz. Nie restartuj niczego.
W ciągu sekundy hub sam podchwyci zmianę i przepisze ją do węzła — w powłoce `diff`
pokaże najpierw `ROZJAZD`, a chwilę później `zgodne`.

### Gdzie postawić pułapkę

| Miejsce | Co widać |
|---|---|
| [`core/bindings.py:63`](lagunhub/core/bindings.py#L63) `BindingEngine.handle` | **ścieżka szybka** — tu ramka z klawisza zamienia się w komendę do przekaźnika, bez dotykania modelu |
| [`core/hub.py`](lagunhub/core/hub.py) `CoreThread.handle` | ścieżka wolna: wszystko, co nie ma budżetu czasu |
| [`core/reconcile.py`](lagunhub/core/reconcile.py) `Reconciler._compare` | moment, w którym hub decyduje, że węzeł ma co innego niż plik |
| [`api/hub.py`](lagunhub/api/hub.py) `HubApi._ask` | granica adaptera: **każda** operacja przechodzi tędy do wątku CORE |

Przy pułapce w `BindingEngine.handle` pamiętaj, że zatrzymujesz wątek RX — transakcje
zaczną się przeterminowywać, bo świat idzie dalej. To nie jest usterka, tylko skutek
zatrzymania jednego z trzech wątków.

## Co da się zobaczyć dzisiaj

### Konfiguracja

W [config/](config/) leżą trzy pliki: magistrale i grupy, węzły z nazwami
i konfiguracją endpointów, powiązania. **Adresów tam nie ma** — adres jest drogą,
nie tożsamością, więc konfiguracja mówi `Uid`, a adres przydziela hub przy
przyłączaniu.

`SIGHUP` przeładowuje konfigurację bez restartu. Przeładowanie, które się nie
powiodło, **zostawia poprzednią w mocy** i mówi, co jest nie tak.

### Cała ścieżka: klawisz → hub → przekaźnik

Tak to wygląda na magistrali. Wydruk jest z `tests/test_e2e.py`, gdzie dwa węzły
**bez adresów** wchodzą do instalacji same, hub je rozpoznaje i doprowadza do zgodności
z konfiguracją, a potem przychodzi kliknięcie:

```
[  0.088] EVENT     0x02A ep4 el=0x03 seq=0 [01]      Click z klawisza
[  0.088] COMMAND   0x040 ep4 el=0x02 tid=1           Toggle od huba
[  0.088] RESPONSE  0x040 ep4 el=0x02 tid=1 OK        przyjąłem i wykonałem
[  0.088] REPORT    0x040 ep4 el=0x00 [01]            a oto skutek fizyczny

dopasowanie powiązań: n=2  p50=22µs  p99=22µs  p99.9=22µs  max=22µs
```

Cztery ramki, nie trzy: odpowiedź mówi „przyjąłem i wykonałem", a raport — jaki
był skutek fizyczny. Przy sprawnym urządzeniu to jedno i to samo; przy zaciętym
styku właśnie tam widać różnicę.

### Awarie — po nich widać, co hub naprawdę robi

Przekaźnik w testach da się zepsuć — `faults.drop_responses = 1` i kliknięcie:

```
  przekaźnik zgubi następną odpowiedź
[  0.090] EVENT     0x02A ep4 el=0x03 seq=0 [01]
[  0.090] COMMAND   0x040 ep4 el=0x02 tid=0
[  0.090] REPORT    0x040 ep4 el=0x00 [01]      ← komenda SIĘ WYKONAŁA
[  0.192] COMMAND   0x040 ep4 el=0x02 tid=0      ← powtórzenie, TEN SAM tid
[  0.192] RESPONSE  0x040 ep4 el=0x02 tid=0 OK   ← z bufora deduplikacji

transakcje: otwarte 1  odpowiedziane 1  powtórzone 1  bez odpowiedzi 0
```

Po powtórzeniu **nie ma drugiego raportu** — bo przekaźnik się nie przełączył
drugi raz. To jest cały sens bufora deduplikacji, widoczny w czterech wierszach.

| Awaria | Co robi przekaźnik | Co widać po stronie huba |
|---|---|---|
| `drop_responses = 1` | zgubi następną odpowiedź | powtórzenie po 100 ms, ten sam `tid` |
| `drop_responses = 9` | zgubi wszystkie | próby o 0/100/200 ms, potem cisza |
| `busy_responses = 1` | odpowie `BUSY` | powtórzenie **natychmiast**, nie po 100 ms |
| `silent_until` | zamilknie na czas | ani odpowiedzi, ani raportów |
| `drop_segment = 1` | zgubi drugi segment | 100 ms ciszy, potem **całe żądanie od nowa** |

### `n` i `a` — jak węzeł wchodzi do instalacji

Węzeł **bez adresu** zgłasza się sam, a wchodzi dopiero przy otwartym trybie
przyjmowania:

```
  dołożony węzeł bez adresu, ShortId 0x60C2886E
[  1.809] JOIN      ZGŁOSZENIE short=…C2886E     ← zgłasza się, ale tryb wyłączony
  tryb przyjmowania włączony na 60 s
[  4.118] JOIN      ZGŁOSZENIE short=…C2886E     ← próbuje dalej, bez końca
[  4.121] JOIN      PRZYDZIAŁ  short=…C2886E
[  4.121] EVENT     0x001 ep0 el=0x01            ← Booted spod NOWEGO adresu
```

Adres `0x001` nie jest przypadkiem: węzeł zadeklarował `NodeClass = CONTROL`,
a to jedyna rzecz, jaką mówi o sobie przed enumeracją — i wystarcza, żeby trafić
do właściwego bloku adresowego.

Zaraz potem hub go **rozpoznaje**: czyta `Uid` (segmentami), tożsamość modelu
i deskryptor endpoint po endpoincie. `N` dokłada panel trzyklawiszowy z podświetleniem
i termometrem — dokładnie ten z [§7 specyfikacji](../lagunbus-spec/01-model-danych.md):

```
transakcje: otwarte 27  odpowiedziane 27  powtórzone 0  bez odpowiedzi 0

0x001  b201020304050607      magistrala 0  READY  słyszany 2.4 s temu
  ep4   KeyInput       ch1
  ep5   KeyInput       ch2
  ep6   KeyInput       ch3
  ep7   Output         ch0 cechy=0x0003
  ep8   Sense          ch0
```

Siedem odczytów na węzeł i cztery na endpoint — hub nie pyta o nic więcej, bo
**odczyt z węzła jest rozstrzygający i jest jedynym źródłem**.

Zrzut modelu pokazuje go osobno, bo hub **nie zna jeszcze jego `Uid`**:

```
Przyłączanie — te węzły mają ShortId, ale hub nie zna jeszcze ich Uid
  ShortId 0x60C2886E  0x001  CONTROL     JOINED
  (Uid odczytuje enumeracja)
```

### `u` — wartość dłuższa niż ramka

`Uid` ma 8–16 bajtów, więc nie mieści się w ośmiu. Naciskasz `u`:

```
[  0.005] READ      0x040 ep0 el=0x00 tid=0
[  0.008] RESPONSE  0x040 ep0 el=0x00 tid=0 SEGMENTED   ← zapowiedź, nie wynik
[  0.008] LONG_RESP 0x040 ep0 el=0x00 [00 00 08 00 08 09 0a 0b]
[  0.008] LONG_RESP 0x040 ep0 el=0x00 [80 01 0c 0d 0e 0f]   ← bit 7 = ostatni
  Uid = 08090a0b0c0d0e0f  (prób: 1)
```

Z `g` przed `u` widać, co się dzieje, gdy segment przepadnie: po 100 ms ciszy hub
powtarza **całe żądanie**, bo retransmisji częściowej nie ma. `prób: 2`.

`b` pokazuje przy tym różnicę, którą łatwo przeoczyć: `BUSY` znaczy „spróbuj
jeszcze raz", więc hub nie czeka na termin — powtarza od razu.

Po wyjściu hub wypisuje, **czego dowiedział się z samego nasłuchiwania**:

```
Model instalacji — 2 węzłów

0x02A  Klawisz w kuchni      magistrala 0  JOINED  słyszany 0.1 s temu
  ep4   KeyInput
0x040  Lampa nad stołem      magistrala 0  JOINED  słyszany 0.1 s temu
  ep4   Output
         OnOff = False
```

Klawisz nie ma ani jednego atrybutu raportowanego, więc hub wie o nim tylko tyle,
że żyje — i tak ma być. Stan wyjścia zna, bo przekaźnik sam go zgłosił.

### Widok pełnoekranowy

Do dłuższej pracy — **strzałki zamiast wpisywania**:

```sh
.venv/bin/lagun-hub tui
```

```
┌─ LagunBus ─ parter ● ─ węzły 2/2 ─ usterek 0 ──────────────────────── 12:20:03 ─┐
│ ▸ Węzły         2   ALIAS           NAZWA              ADR    STAN     EP       │
│   Powiązania       ▸kitchen-switch  Klawisz w kuchni   0x002  READY     4       │
│   Rozjazd       1   kitchen-relay   Lampa nad stołem   0x001  READY     5       │
│   Usterki                                                                       │
│   Przyłączanie     ── kitchen-relay · Lampa nad stołem · kuchnia ────────────── │
│   Kondycja          EP   TYP     KANAŁ  NAZWA             CO W NIM JEST         │
│                    ▸ep0  Device  0      —                 —                     │
│                     ep4  Output  1      Lampa nad stołem  OnOff = nie           │
│                      [t] Toggle [o] On [f] Off [i] Identify [R] uzgodnij        │
├─ Ruch na magistrali ─ 3 ramek/s ─ [spacja] pauza ───────────────────────────────┤
│   61.205 → hub    KOMENDA     0x001 ep4   Toggle                 tid=1          │
│   61.208   hub    ODPOWIEDŹ   0x001 ep4   Toggle OK              tid=1  3.1 ms  │
└─────────────────────────────────────────────────────────────────────────────────┘
 ↑↓ wybór   → w głąb   ← wstecz   ⇥ sekcja   r odśwież   ? pomoc   q wyjście
```

| Klawisz | |
|---|---|
| `↑` `↓` `PgUp` `PgDn` | wybór — po **każdej** liście, także po sekcjach |
| `→` / `⏎` | w głąb: sekcje → lista → endpointy → atrybuty |
| `←` / `⌫` / `Esc` | z powrotem; na samej górze nie robi nic |
| `⇥` | następna sekcja (jedyny ruch, który przeskakuje kontekst) |
| `t` `o` `f` | Toggle / On / Off na wybranym wyjściu |
| `c` | wyślij dowolną komendę — z listy, z argumentami |
| `i` | Identify — która to puszka |
| `n` `N` | nazwa / alias węzła (idą do pliku) |
| `e` `w` | edytuj **w pliku** / nastaw **w węźle** |
| `u` `g` `p` | usuń z pliku / odczytaj z węzła / przyjmij z węzła do pliku |
| `R` | uzgodnij ten węzeł teraz |
| `+` `-` | dodaj / usuń powiązanie |
| `/` `spacja` `r` `?` | filtr / pauza podglądu / odśwież / pomoc |
| `q` | **jedyne** wyjście — klawisz nawigacji nigdy nie zamyka widoku |

**Lista nigdy nie znika.** Wejście w węzeł zmienia to, co pod nią — nie chowa instalacji.
Nadmiar wysokości dostaje lista, nie panel szczegółów.

Podpowiedzi pod szczegółami **zmieniają się wraz z tym, co wybrane**: `t Toggle` pojawia
się tylko wtedy, gdy jest czym przełączać, a `e edytuj w pliku` tylko na atrybucie,
który jest konfiguracją.

Wartości nazwane i logiczne **wybiera się z listy**, nie wpisuje — bo przy `bool` nie
ma skąd wiedzieć, czy hub chce `true`, `1`, czy `on`. Liczby wpisuje się z wypisanym
zakresem (`u16 · 0…65535`).

Skąd ten układ i czego w nim jeszcze brakuje — [`ARCHITEKTURA.md`](ARCHITEKTURA.md) §10.3.3.

Kolory niosą treść: węzeł bez adresu jest przygaszony, rozjazd z plikiem i powiązanie
bez celu są czerwone, zgodność zielona.

### Powłoka

```sh
.venv/bin/lagun-hub run --config config    # okno 1
.venv/bin/lagun-hub shell                  # okno 2
```

Powłoka **nie startuje huba — dołącza się do chodzącego**. Na tej samej maszynie adres
i token bierze sama z `config/`, więc zwykle wystarcza `lagun-hub shell`.

```
lagun> nodes
ALIAS           NAZWA             UID               ADR    STAN   EP
kitchen-switch  Klawisz w kuchni  0001020304050607  0x002  READY  1
kitchen-relay   Lampa nad stołem  08090a0b0c0d0e0f  0x001  READY  1

lagun> cmd kitchen-relay ep4 Toggle
OK  (1 prób, 0.9 ms)

lagun> diff
WĘZEŁ          EP   ATRYBUT  W PLIKU  W WĘŹLE  STAN
kitchen-relay  ep4  Purpose  LIGHT    VALVE    ROZJAZD
```

Dwa polecenia wyglądają podobnie i robią coś zupełnie innego — dlatego `help` mówi
to wprost:

| | |
|---|---|
| `set relay ep4 AutoOffTime 30` | **konfiguracja** → do pliku, a do węzła uzgadnianiem |
| `write relay ep4 Level 128` | **nastawa robocza** → wprost do węzła, pliku nie dotyka |

`feed on` włącza podgląd zdarzeń pod zachętą, `watch` strumieniuje do `Ctrl-C`.

**Tabulator uzupełnia nad żywą instalacją.** Aliasy, numery endpointów, nazwy komend
i dozwolone wartości biorą się z odpowiedzi huba — powłoka nie wie o typie `Output`
niczego:

```
lagun> cmd kitchen-relay ep4 Tog⇥          →  Toggle
lagun> set kitchen-relay ep4 Purpose ⇥     →  GENERIC  INDICATOR  LIGHT  VALVE
lagun> set kitchen-relay ep4 ⇥             →  tylko to, co JEST konfiguracją
```

Ostatni wiersz jest sprawdzianem samego API: `OnOff` to stan, a `Level` nastawa robocza,
więc pod `set` się nie pojawiają — podpowiedzenie ich prowadziłoby prosto w błąd
„to jest stanem, nie konfiguracją".

### API huba

`lagun-hub run` wypisuje adres serwera. Token hub losuje przy pierwszym starcie
i zapisuje do `config/api-token` z prawami `0600` — wypisany jest wtedy raz i tylko
wtedy.

```sh
API=http://127.0.0.1:8080
TOKEN=$(cat config/api-token)
curl -H "Authorization: Bearer $TOKEN" $API/nodes
curl -H "Authorization: Bearer $TOKEN" $API/nodes/kitchen-relay/ep/4
curl -X POST -H "Authorization: Bearer $TOKEN" \
     $API/nodes/kitchen-relay/ep/4/commands/Toggle
curl -N "$API/events?token=$TOKEN"   # zdarzenia na żywo
```

Bez tokenu każde żądanie dostaje `401` — **także strumień zdarzeń**. Hub steruje
rzeczami fizycznymi, więc trybu bez uwierzytelniania po prostu nie ma.

Dwa wiersze odpowiedzi warto zobaczyć od razu. Pierwszy: `matches` przy każdym
atrybucie mówi, czy węzeł ma to, co stoi w pliku — rozjazd między konfiguracją
a rzeczywistością jest widoczny wprost, a nie do wywnioskowania. Drugi: `choices`
niesie dozwolone wartości (`LIGHT`, `VALVE`, …), więc lista rozwijana we froncie
bierze się z huba, a nie z przepisanej gdzie indziej tabelki.

```sh
# co węzeł odpowiada, gdy nie chce:
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Authorization: Bearer demo' \
     $API/nodes/kitchen-relay/ep/4/commands/Toggle
# 200 — zrobione
# 409 — węzeł odmówił (kod wyniku w ciele)
# 504 — węzeł nie odpowiedział po trzech próbach
# 503 — węzeł nie ma adresu, nie ma jak z nim rozmawiać
# 500 — nie odpowiedział HUB; to jest usterka po naszej stronie
```

### Zmiana konfiguracji przez API

Zapis **idzie do pliku, nie do węzła**. Do węzła trafia uzgadnianiem — i to jest jedyna
droga, bo druga sprawiłaby, że plik przestaje opisywać instalację, a zaczyna opisywać
to, co ktoś kiedyś w nim napisał ([`KONFIGURACJA.md`](KONFIGURACJA.md) §11.1).

```sh
# wersja pliku — bez niej nie da się nic zapisać
ETAG=$(curl -sD- -o /dev/null -H "$H" $API/config | grep -i '^etag:' | cut -d' ' -f2)

curl -X PUT -H "$H" -H "If-Match: $ETAG" -d '{"value": 30}' \
     $API/config/nodes/08090a0b0c0d0e0f/endpoints/4/AutoOffTime
```

Ścieżka jest ścieżką **w dokumencie**, więc węzeł występuje w niej pod `Uid`-em,
a nie pod aliasem: alias jest etykietą i wolno go zmienić, a adres zapisu nie może
zależeć od czegoś, co da się zmienić.

```
400  bez If-Match — zapis na ślepo; najpierw GET, potem PUT
409  ktoś zmienił plik w międzyczasie (edytor albo druga karta)
422  konfiguracja się nie broni; NIC nie zapisano
200  zapisane, nowy ETag w nagłówku
```

Trzy rzeczy warto sprawdzić samemu, bo to one odróżniają zapis konfiguracji od
zwykłego `POST`-a:

```sh
# 1. komentarze i cudzysłowy przeżywają zapis z aplikacji
grep -n '#' config/nodes.yaml

# 2. wartość dojeżdża do węzła sama, przez uzgadnianie
curl -s -H "$H" $API/nodes/kitchen-relay/ep/4 | grep -o '"AutoOffTime".\{0,60\}'

# 3. ręczna edycja pliku jest podchwycona w sekundę, bez restartu
sed -i '' 's/Purpose: LIGHT/Purpose: VALVE/' config/nodes.yaml
curl -s -H "$H" $API/diff
```

Plik, którego nie da się wczytać, **zostawia poprzednią konfigurację w mocy** i dopisuje
usterkę do `/issues`. Zła konfiguracja nie ma prawa zatrzymać działającej instalacji.

### Rejestr identyfikatorów, jaki hub widzi

```sh
.venv/bin/lagun-hub registry
```

Wypisuje rodzaje wiadomości, kody wyniku, bloki adresowe, endpointy systemowe,
typy endpointów i atrybuty wspólne — wszystko **wygenerowane** z
[`lagunbus-spec/registry/`](../lagunbus-spec/registry/), nie przepisane ręcznie.

### Na prawdziwej magistrali (Linux)

```sh
sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
# prawdziwa magistrala zamiast wirtualnej:
#   sudo ip link set can0 type can bitrate 500000 && sudo ip link set up can0

.venv/bin/lagun-hub run --config config/ --bus vcan0
```

Drugą stronę kontraktu dają dziś **prawdziwe węzły** — patrz
[`../devices/`](../devices/).

`lagun-hub monitor --bus vcan0` pokazuje sam ruch, bez wykonywania powiązań.

## Raspberry Pi

Wszystko w tej sekcji wykonuje się **na RPi**, z nakładką `MCP2518FD`.

**Hub chodzi w powłoce, z katalogu klonu.** Usługi `systemd` nie ma i na razie nie ma jej
mieć: to, co się zmienia kilka razy dziennie, łatwiej zatrzymać `Ctrl-C` niż `systemctl`.
Usługa przyjdzie razem z dziennikiem i wykrywaniem nieobecności
([`ARCHITEKTURA.md`](ARCHITEKTURA.md) §14.4).

### Magistrala — zanim cokolwiek innego

To jedyna część, która dotyka systemu na trwałe, i jedyna, której żaden skrypt tu
**nie robi za ciebie**: wymaga restartu i zależy od tego, co masz wlutowane.

#### SPI i nakładka

`MCP2518FD` wisi na SPI, więc trzeba go opisać jądru. W `/boot/firmware/config.txt`
(na starszych systemach `/boot/config.txt`):

```
dtparam=spi=on
dtoverlay=mcp251xfd,spi0-0,interrupt=25,oscillator=40000000
```

> **Rezonator sprawdź na płytce, nie w internecie.** Spotyka się 20 i 40 MHz. Zła
> wartość daje magistralę, która „prawie" działa: prędkość dwa razy nie ta, co trzeba,
> i ramki, których nikt nie rozumie. To najdroższa pomyłka w tym dokumencie, bo wygląda
> jak usterka gdzie indziej.

Numer przerwania i wybór `spi0-0` / `spi0-1` zależą od nakładki. Co ta nakładka
przyjmuje, wypisuje `/boot/overlays/README` — szukaj `mcp251xfd`.

```bash
sudo reboot
```

#### Czy wstał

```bash
dmesg | grep -i mcp            # sterownik się odezwał?
ip -details link show can0     # jest interfejs? co umie?
```

`can state STOPPED` jest w porządku — interfejs jest, tylko nie podniesiony. Czego
**nie** ma być: ciszy w `dmesg`. Cisza znaczy, że nakładka się nie wczytała albo SPI
jest wyłączone.

W `ip -details` przeczytaj listę trybów (`listen-only`, `loopback`, `presume-ack`) —
stąd wiadomo, **co da się sprawdzić, zanim będzie drugi węzeł**.

#### Podniesienie

```bash
sudo ./tools/can-up.sh can0 500000
```

albo ręcznie, bo to jedno polecenie:

```bash
sudo ip link set can0 up type can bitrate 500000 restart-ms 100
```

> **Bez `fd on`.** Ten kontroler umie CAN FD i chętnie by go użył, a specyfikacja
> LagunBus mówi CAN 2.0B ([`05-mapowanie-can.md`](../lagunbus-spec/05-mapowanie-can.md)).

Po restarcie RPi trzeba to powtórzyć — na razie ręcznie i to jest w porządku.

#### Magistrala wirtualna — do prób bez sprzętu

```bash
sudo ./tools/can-up.sh vcan0
```

Nie zastępuje nakładki. Służy do **rozdzielania usterek**: co nie zadziała na `vcan0`,
jest sprawą Linuksa albo pakietu, a nie `MCP2518FD`.

---

### Instalacja huba

```bash
git clone <adres-repozytorium> ~/hub
cd ~/hub
./tools/setup-rpi.sh
```

Skrypt instaluje `python3-venv` i `can-utils`, robi `.venv`, wkłada pakiet
**edytowalnie** (`git pull` wystarcza do aktualizacji) i zakłada **czystą konfigurację
w `~/lagun`**. Na koniec uruchamia `doctor`.

Bez skryptu to są cztery polecenia:

```bash
sudo apt install python3-venv can-utils
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/lagun-hub init ~/lagun --bus can0
```

#### Konfiguracja leży POZA klonem

> **`config/` w repozytorium jest przykładem do czytania.** Ma w sobie dwa konkretne
> `Uid`, które nie istnieją nigdzie poza testami, i zderzałaby się z `git pull`
> przy każdej aktualizacji.

Prawdziwa instalacja zaczyna się pusta i zapełnia sama: węzły wchodzą przez
przyłączanie, a nazwy nadaje im człowiek z widoku.

```bash
echo 'export LAGUN_CONFIG=$HOME/lagun' >> ~/.bashrc && exec bash
```

Od tej pory żadne polecenie nie potrzebuje `--config`. Kolejność jest taka:
`--config` bije `$LAGUN_CONFIG`, a ten bije `./config`.

#### Uruchomienie

```bash
.venv/bin/lagun-hub run
```

Hub trzyma terminal i wypisuje ruch na magistrali. **Token wypisze się raz**, przy
pierwszym starcie — potem leży w `~/lagun/api-token` z prawami `0600`.

`Ctrl-C` zatrzymuje i wypisuje podsumowanie: histogram opóźnień, liczniki transakcji
i stan instalacji.

Do dłuższej pracy — `tmux` albo `screen`, bo inaczej hub ginie razem z `ssh`:

```bash
tmux new -s hub
.venv/bin/lagun-hub run
# Ctrl-B D odczepia, `tmux attach -t hub` wraca
```

Z drugiego terminala, na tej samej maszynie:

```bash
.venv/bin/lagun-hub tui
```

#### Dostęp z laptopa

Domyślnie API słucha na pętli lokalnej. W `~/lagun/hub.yaml`:

```yaml
api:
  host: 0.0.0.0
  port: 8080
```

> **Token jest wtedy jedyną ochroną.** Nie ma kont ani ról — jeden token na hub, i kto
> go ma, steruje domem ([`ARCHITEKTURA.md`](ARCHITEKTURA.md) §13, H6).

Z laptopa:

```bash
export LAGUN_TOKEN=$(ssh rpi cat ~/lagun/api-token)
lagun-hub tui --url http://rpi.local:8080
```

#### Aktualizacja

```bash
cd ~/hub && git pull
.venv/bin/lagun-hub run
```

Instalacja jest edytowalna, więc `pip` nie jest potrzebny — chyba że zmieniły się
zależności. Konfiguracja leży poza klonem, więc **`git pull` nigdy się z nią nie
zderzy**.

---

### Cała ścieżka bez sprzętu

To jest sprawdzian, po który warto sięgnąć **zanim dojdzie nakładka**: jeśli tu coś
nie działa, jest to sprawą Linuksa albo pakietu, a nie `MCP2518FD`.

Trzy terminale (albo trzy okna `tmux`):

```bash
# 1 — magistrala wirtualna i hub
sudo ./tools/can-up.sh vcan0
lagun-hub init ~/lagun-proby --bus vcan0
LAGUN_CONFIG=~/lagun-proby lagun-hub run

# 2 — widok
LAGUN_CONFIG=~/lagun-proby lagun-hub tui
```

Drugą stronę magistrali dają **prawdziwe węzły** ([`../devices/`](../devices/)).
Startują **bez adresu** i zgłaszają się same. W widoku: klawisz `[a]` otwiera
przyjmowanie, po chwili pojawiają się na liście. Nadaj im aliasy (`[N]`), zwiąż
klawisz z przekaźnikiem (`[+]` w sekcji Powiązania) — i to jest cała ścieżka.

> Symulowanych węzłów do uruchamiania z ręki na razie nie ma — wyszły z huba, bo na RPi
> ma lądować wyłącznie hub; wrócą jako oddzielny projekt (`zadania/FEAT-02.md`).

---

### Czy wszystko gotowe

```bash
.venv/bin/lagun-hub doctor
```

Wypisuje, co jest gotowe, a co nie — **razem z poleceniem, które to naprawia**. Kod
wyjścia zero znaczy „startuj". Wiersz `interfejs can0` mówi, czy **gniazdo naprawdę
się otwiera**; to jedyne sprawdzenie, które robi dokładnie to, co zrobi hub.

Magistrala od drugiej strony, bez huba:

```bash
candump can0                  # co jedzie
cansend can0 123#DEADBEEF     # ramka z palca
```

> `cansend` na magistrali, na której nie ma nikogo innego, **nie przejdzie**. CAN
> wymaga, żeby ramkę potwierdził ktoś w szczelinie `ACK`; samotny nadajnik powtarza
> i schodzi w `bus-off`. Odbiór działa zawsze — nadawanie dopiero we dwóch.

---

### Gdy nie działa

| Objaw | Gdzie szukać |
|---|---|
| `dmesg` milczy o `mcp` | nakładka nie wczytana albo brakuje `dtparam=spi=on` |
| jest `can0`, ale `doctor` mówi „bez taktowania" | interfejs wstał bez `bitrate` — `can-up.sh` |
| `doctor`: gniazdo się nie otwiera | uprawnienia; sprawdź, na jakim użytkowniku pracujesz |
| hub wstaje, ale nie widzi węzłów | najpierw `candump can0` — jeśli tam cisza, to nie hub |
| ramki są, ale bez sensu | **rezonator**; sprawdź `oscillator=` w `config.txt` |
| hub kończy z kodem 2 | konfiguracja — komunikat powie, która linia |

Zasada, która oszczędza wieczory: **`candump` przed hubem**. Jeśli `candump` nic nie
widzi, żadna ilość patrzenia w hub nie pomoże.

### Co pierwsze uruchomienie ma pokazać

Na macOS kod SocketCAN **nigdy się nie wykonał**, więc pierwsze uruchomienie na RPi jest
sprawdzianem, a nie formalnością — lista niesprawdzonych rzeczy i powody są
w [`ARCHITEKTURA.md`](ARCHITEKTURA.md) §14.1.

```
✓ can0 wstaje po restarcie RPi, bez ręcznego modprobe
✓ ip -details link show can0 pokazuje 500 kbit/s i to, co umie sterownik
✓ lagun-hub monitor wstaje na can0 i nie wywraca się na braku ruchu
✓ token wylosował się sam, a plik ma prawa 0600
✓ tui przez ssh działa i nie zostawia śmieci w terminalu
✓ pierwsza ramka z węzła dociera do huba i widać ją w /frames z nazwami
✓ klawisz → hub → przekaźnik na prawdziwych węzłach, opóźnienie ZMIERZONE na RPi
  i zapisane obok liczby z macOS
✓ odpięcie kabla nie kładzie huba; wpięcie z powrotem wraca BEZ restartu
✓ zalanie magistrali ruchem daje ENOBUFS, a hub gubi ramki i MÓWI ILE
```

Do nadawania wystarczy **jedna płytka z podniesionym TWAI** — potwierdzenie w szczelinie
`ACK` wystawia kontroler, nie aplikacja, więc ESP32, który umie tylko mrugać diodą,
wystarczy, żeby hub mógł nadawać. Po takim uruchomieniu wiadomo też, czy `VirtualBus` ma
udawać `bus-off` (**H4**), bo wiadomo, jak to wygląda naprawdę.

## Sprawdzenie

```sh
.venv/bin/python -m pytest -q          # 502 testy
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy lagunhub tools tests
```

Testy nie czekają na zegar: atrapa węzła dostaje zegar sterowany z testu, więc
półtorasekundowe przytrzymanie klawisza trwa mikrosekundy.

### Czym hub rozmawia w testach

Po drugiej stronie kontraktu stoi **atrapa węzła** w [`tests/node/`](tests/node/):
mówi tym samym protokołem, czyta ten sam katalog typów i daje się psuć (gubienie
odpowiedzi i segmentów, `BUSY`, cisza). Przed wersją 0.5 był to symulator pakowany razem
z hubem — wyszedł z paczki, bo na Raspberry Pi ma lądować **wyłącznie hub**.

Czego atrapa nie robi: nie ma pamięci trwałej, nie zmienia adresu po `SetAddress`
i nie da się jej uruchomić osobno. Symulator do uruchamiania z ręki będzie osobnym
projektem obok tych repozytoriów.

### Rejestr ma się zgadzać ze specyfikacją

```sh
.venv/bin/python tools/generate_registry.py --check    # psuje build przy rozjeździe
.venv/bin/python tools/generate_registry.py            # przegenerowanie
```

To jest zarazem test: `tests/test_registry.py` uruchamia `--check`, więc zmiana
w rejestrze specyfikacji bez przegenerowania pliku **zapala się na czerwono**.
