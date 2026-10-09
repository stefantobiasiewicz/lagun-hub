# Architektura huba LagunBus

> **Szkic, v0.2.** Dokument opisuje **jak hub jest zbudowany w środku**. Co hub kładzie na
> magistralę, rozstrzyga [`lagunbus-spec/07-hub.md`](../lagunbus-spec/07-hub.md) i to on jest
> nadrzędny — tutaj nie ma ani jednego zdania normatywnego o ramce.

Python 3.12, SocketCAN, docelowo Raspberry Pi z nakładkami CAN. Rozwój na macOS przez
transport wirtualny (§9.2). **Dokumentacja po polsku, kod i nazwy po angielsku.**

---

## 1. Zakres

Hub robi dokładnie to, co wymienia [`07-hub.md`](../lagunbus-spec/07-hub.md) §1:

| Obowiązek | Gdzie w tym dokumencie |
|---|---|
| Nadawać i pilnować adresów | §8 |
| Prowadzić model instalacji | §6 |
| Wykonywać powiązania | §4 |
| Wykrywać nieobecność węzłów | §7.3 |
| Wystawiać instalację na zewnątrz | §10 |

**Czego nie robi:** harmonogramów, warunków, pory dnia, trybów domu. To jest zadanie systemu
nadrzędnego ([`00-wprowadzenie.md`](../lagunbus-spec/00-wprowadzenie.md) § Czego ten system
nie robi). Hub wystawia instalację przez MQTT i na tym kończy swoją rolę.

Skąd hub bierze powiązania, źródła danych, konfigurację endpointów i ustawienia adapterów —
[`KONFIGURACJA.md`](KONFIGURACJA.md).

Drugą stroną kontraktu w testach jest **atrapa węzła** w [`tests/node/`](tests/node/).
Przed wersją 0.5 był to symulator pakowany razem z hubem; wyszedł z paczki, bo na Raspberry Pi
ma lądować wyłącznie hub (§12.2). The simulator run by hand is now a project of its own,
[`../lagun-sim/`](../lagun-sim/), on the node library's C code; it reaches the hub over
`UdpBus` (§9) and shares no code with it.

---

## 2. Decyzje

| Decyzja | Wybór | Dlaczego |
|---|---|---|
| Język | **Python 3.12**; kod po angielsku — nazwy, komentarze, docstringi, komunikaty | dokumentacja `.md` zostaje po polsku; po polsku zostają tylko nazwy wzięte ze specyfikacji (klucze tablicy tłumaczącej rejestr, pola diagramu identyfikatora) |
| Współbieżność | **wątki + kolejki** | wątek RX blokuje na `select()` i nie czeka na nic innego |
| Dostęp do CAN | **surowy `AF_CAN`/`SOCK_RAW`** | zero zależności na ścieżce ramki, pełna kontrola nad trybem nieblokującym |
| Podział pracy | **ścieżka szybka / ścieżka wolna** | §3 — nie po to, żeby zmieścić się w mikrosekundach, tylko żeby nic wolnego nie zatrzymało klawisza |
| Dostęp do modelu z RX | **niezmienny snapshot, copy-on-write** | prostsze niż lock i przy okazji szybsze |
| Nadawanie | **każdy wątek nadaje sam, bez wątku TX** | `send()` pojedynczej ramki jest niepodzielny; wątek TX dokładałby kolejkę bez zysku |
| Trwałość | **the configuration file** for what must survive a restart (`joined`, §8.6); SQLite with a delayed write is written (`core/store/`) and **not connected** | what the hub cannot read back from the bus is an address and a `ShortId` — they fit in the node's entry; the database waits for a reason of its own (`zadania/FEAT-07.md`) |
| Adaptery | **klient w terminalu → REST → MQTT**, wszystkie nad jednym API rdzenia | §10.2 — pierwszy nie wymagał ani serwera, ani brokera; dziś w terminalu jest tylko widok pełnoekranowy, powłoki nie ma (§10.3) |
| Serwer API | **biblioteka standardowa**, `ThreadingHTTPServer` (**H7**) | zero zależności na RPi; czy tak zostaje — `zadania/CR-03.md` |
| API rdzenia | **pakiet `api/`: kontrakt osobno, implementacja ładowana leniwie** | klient chodzącego huba nie ładuje rdzenia; pilnuje `tests/test_boundaries.py` (§12) |
| Druga strona w testach | **atrapa węzła w `tests/node/`**, poza paczką | na Raspberry Pi ma lądować wyłącznie hub; the simulator run by hand is `../lagun-sim`, a separate project |
| Źródło identyfikatorów | **`lagunbus-spec/registry/*.yaml`** | generujemy z nich kod, nie przepisujemy ręcznie |

---

## 3. Idea naczelna: dwie ścieżki

Ruch przechodzący przez hub dzieli się na dwa rodzaje i dzieli się **nie po ważności, tylko
po tym, jak długo wolno mu zająć**.

Naciśnięcie klawisza ma dać światło od razu — człowiek zauważa opóźnienie gdzieś od
stu milisekund i wtedy przełącznik zaczyna sprawiać wrażenie zepsutego. Enumeracja nowego
urządzenia, zapis do bazy, ponowne łączenie z brokerem MQTT czy odczyt segmentowanego `Uid`
nie mają żadnego takiego ograniczenia i mogą trwać sekundy.

> **Wątek, który odbiera ramki, nigdy nie czeka na nic poza magistralą.**
> Wszystko, co może potrwać — I/O, baza, sieć, enumeracja — dzieje się gdzie indziej.

To jest cała zasada. Nie chodzi o walkę o mikrosekundy (§5), tylko o to, żeby zawieszony
broker MQTT albo wolna karta SD nie miały **żadnej drogi**, którą mogłyby zatrzymać
przekaźnik.

```
                            magistrale CAN
             can0            can1            can2
              │               │               │
              └───────────────┼───────────────┘
                              │
                   ┌──────────▼───────────┐
                   │      WĄTEK RX        │   select() nad wszystkimi socketami
                   │  dekoduj 29-bit ID   │   nigdy nie czeka na nic poza magistralą
                   └──────┬────────┬──────┘
                          │        │
        ┌─────────────────┘        └──────────────────┐
        │                                             │
 ŚCIEŻKA SZYBKA                                ŚCIEŻKA WOLNA
 ZDARZENIE · ALARM · ZAPYTANIE                 RAPORT · ODPOWIEDŹ · PRZYŁĄCZANIE
                                               segmenty · diagnostyka
        │                                             │
 ┌──────▼────────────────┐                     ┌──────▼──────┐
 │ snapshot (niezmienny) │                     │   Queue     │
 │  addr  → Uid          │                     └──────┬──────┘
 │  (Uid,ep,el) → cele   │                            │
 │  (Uid,ep,el) → wart.  │                     ┌──────▼──────────────────┐
 └──────┬────────────────┘                     │      WĄTEK CORE         │
        │                                      │  model instalacji       │
   send() wprost do socketu                    │  transakcje + timery    │
        │                                      │  enumeracja, obecność   │
        ▼                                      └───┬──────────────┬──────┘
   kilkadziesiąt mikrosekund,                      │              │
   bez I/O i bez czekania                 ┌────────▼────┐  ┌──────▼──────┐
                                          │ WĄTEK STORE │  │ WĄTEK ADAPT.│
                                          │   SQLite    │  │ API / MQTT  │
                                          └─────────────┘  └─────────────┘
```

Strzałka z CORE do snapshotu jest jedyną drogą w drugą stronę: CORE buduje nowy snapshot
i podmienia referencję (§5.3). RX nigdy nie pisze do modelu.

### 3.1 Co jest szybkie, a co wolne

| `RODZAJ` | Ścieżka | Dlaczego |
|---|---|---|
| `ZDARZENIE` `0x1` | **szybka** | klawisz → przekaźnik; to jest cały powód istnienia projektu |
| `ALARM` `0x0` | **szybka** | najwyższy priorytet na magistrali, więc i w hubie |
| `ZAPYTANIE` `0xB` | **szybka** | węzeł prowadzi transakcję ze 100 ms budżetem i zaraz powtórzy ([`04`](../lagunbus-spec/04-interakcje.md) §8.2) |
| `RAPORT` `0x8` | wolna | raport spóźniony o sekundę nadal niesie prawdę ([`04`](../lagunbus-spec/04-interakcje.md) §7.2) |
| `ODPOWIEDŹ` `0x7` | wolna | budżet 100 ms, a hub i tak tylko kojarzy ją z żądaniem |
| `ODPOWIEDŹ_DŁUGA` `0x9` | wolna | dane masowe, z definicji stoją za raportami |
| `PRZYŁĄCZANIE` `0xF` | wolna | zdarza się przy pierwszym uruchomieniu instalacji |

Ramka szybka trafia **także** do kolejki wolnej — ale dopiero po nadaniu komendy. Dziennik,
MQTT i licznik kolejny zdarzeń są sprawą CORE i nie mają prawa opóźnić przekaźnika.

### 3.2 Powiązanie nie wraca do silnika

[`07-hub.md`](../lagunbus-spec/07-hub.md) §4.1 zabrania łańcuchów. W tej architekturze wynika
to z budowy, a nie z warunku w kodzie: **silnik powiązań karmi się wyłącznie ramkami
odebranymi z socketu**, a ramka nadana przez hub nigdy przez niego nie przechodzi. Raport,
który przyjdzie po komendzie, jest ścieżką wolną i też nie jest źródłem.

Pętla jest więc niemożliwa do zbudowania, a nie tylko zabroniona.

---

## 4. Ścieżka szybka w szczegółach

Wszystko poniżej dzieje się w wątku RX, między `recv()` a `send()`.

```
                            ramka z socketu
                                  │
                      ┌───────────▼────────────┐
                      │  dekoduj identyfikator │   jedno przesunięcie i maska ×4
                      │  RODZAJ ADRES EP ELEM  │
                      └───────────┬────────────┘
                                  │
                 RODZAJ ∈ {ALARM, ZDARZENIE} ?
                                  │ tak
                      ┌───────────▼────────────┐
                      │ snap = self.snapshot   │   jeden odczyt referencji
                      │ uid = snap.addr[ADRES] │   dict lookup
                      └───────────┬────────────┘
                                  │
                      ┌───────────▼─────────────────────┐
                      │ cele = snap.bind[(uid,EP,ELEM)] │   dict lookup
                      └───────────┬─────────────────────┘
                                  │  brak → tylko do kolejki, to nie błąd
                      ┌───────────▼────────────┐
                      │ dla każdego celu:      │
                      │   ramka = cel.gotowa   │   PREKOMPILOWANA przy konfiguracji
                      │   wstaw Tid            │   jeden bajt
                      │   sock[cel.bus].send() │
                      └───────────┬────────────┘
                                  │
                        kolejka_wolna.put_nowait()
```

### 4.1 Prekompilowana ramka celu

Cel powiązania jest znany przy budowie snapshotu: wiadomo, który to węzeł, pod jakim
**bieżącym** adresem, jaki endpoint, jaka komenda i jakie argumenty. Zmienny jest
**wyłącznie `Tid`** — trzy bity w bajcie `[0]`. Więc CORE buduje gotowy `bytes` całej
ramki i wkłada go do snapshotu. Ścieżka szybka nie koduje niczego.

> **Prekompilacja dzieje się przy snapshocie, nie przy wczytaniu konfiguracji.**
> Konfiguracja opisuje cel przez `Uid` (`BindingSpec`), bo `Uid` jest tożsamością,
> a adres drogą. Ramka złożona raz, przy starcie, przeżyłaby przeadresowanie węzła
> i dalej wskazywała cudzy adres — usterkę widać byłoby dopiero wtedy, gdy zapali się
> nie to światło.

```
konfiguracja                            snapshot (niezmienny)
  klawisz 0x02A ep4 Click       ──►  bind[(uid_A, 4, 0x03)] = [
  → przekaźnik 0x040 ep4 Toggle             Target(bus=1,
                                                   can_id=0x3_040_04_02,
                                                   payload=bytearray(b'\x00'))
                                          ]
ścieżka szybka:  payload[0] = tid  →  send()
```

Robimy to nie dlatego, że inaczej byłoby za wolno — dlatego, że **kodowanie ramki w jednym
miejscu, przy konfiguracji, jest prostsze do sprawdzenia** niż kodowanie jej przy każdym
naciśnięciu klawisza. Szybkość jest skutkiem ubocznym, nie celem.

### 4.2 Podział puli `Tid`

Komenda jest transakcją, więc wymaga `Tid` i powtórzeń. `Tid` ma trzy bity, czyli osiem
wartości na parę (węzeł, endpoint, komenda) ([`04`](../lagunbus-spec/04-interakcje.md) §2.2).
Żądania wychodzą z dwóch wątków naraz: z RX (powiązania) i z CORE (odczyty, zapisy,
enumeracja).

> **Pula jest jedna, wspólna dla wszystkich wątków, i obejmuje wszystkie osiem wartości.**

Podział na pół — `0`–`3` dla ścieżki szybkiej, `4`–`7` dla CORE — wyglądał elegancko
i **był błędem**. Dawał hubowi limit **czterech** tych samych komend do tego samego
endpointu na 300 ms, a specyfikacja budżetuje **sześć** i liczy dokładnie ten przypadek:
`Step` co 50 ms przy ściemnianiu przytrzymaniem klawisza
([`04`](../lagunbus-spec/04-interakcje.md) §2.2). Powiązanie `HoldTick` → `Step` przy
`HoldTickInterval = 50 ms` przekroczyłoby go, a komendy zaczęłyby **znikać po cichu**:
węzeł odsyłałby zapamiętaną odpowiedź zamiast wykonać krok.

Wspólny przydział obywa się przy tym **bez locka** — opiera się na dwóch niepodzielnych
operacjach CPythona, `dict.setdefault` i `next()` na `itertools.count`. Jest to ta sama
klasa założenia co podmiana referencji snapshotu (§5.3).

Kluczem jest **identyfikator ramki żądania**, czyli dokładnie to, co węzeł bierze do
bufora deduplikacji: adres, endpoint i numer elementu. Osiem komend do ośmiu różnych
endpointów nie koliduje więc ze sobą, choćby poszły w pięć milisekund.

Powtórzenia obsługuje CORE: RX nadaje komendę **i zgłasza ją do kolejki wolnej** jako
`Sent`. Pierwsze powtórzenie wypada 100 ms później, więc nie jest sprawą pilną — a RX
nie musi prowadzić ani jednego terminu.

Rozróżnienie `Received` / `Sent` w kolejce wolnej jest przy tym potrzebne z drugiego
powodu, który wychodzi dopiero na prawdziwym sprzęcie: **gniazdo SocketCAN nie odbiera
własnych ramek**. Podgląd karmiony samym odbiorem pokazywałby zdarzenie i odpowiedź,
a komendę między nimi połykał — czyli kłamał dokładnie o tym, co hub zrobił. Na
magistrali wirtualnej nie byłoby tego widać, bo tam podgląd siedzi na osobnym porcie.

### 4.3 Wiele celów

Jedno źródło może mieć wiele celów ([`07-hub.md`](../lagunbus-spec/07-hub.md) §4). Silnik
wykonuje je w kolejności konfiguracji, w pętli po liście z snapshotu. Gdy cele mają ruszyć
naprawdę jednocześnie, właściwym narzędziem jest **komenda grupowa** — i wtedy lista ma
jeden element, a ramka jedzie na każdą magistralę, na której są członkowie grupy
([`07-hub.md`](../lagunbus-spec/07-hub.md) §6). Które to magistrale, wie snapshot; ścieżka
szybka niczego nie wylicza.

### 4.4 Zapytanie

`ZAPYTANIE` jest szybkie z innego powodu niż zdarzenie: nie dlatego, że ktoś czeka na światło,
tylko dlatego, że **węzeł prowadzi transakcję ze 100 ms budżetem** i zaraz powtórzy.

```
ZAPYTANIE (adr, ep, elem, Tid)
        │
   uid = snap.addr[adr]
   wart = snap.inputs.get((uid, ep, elem))
        │
   jest    → ODPOWIEDŹ_HUBA OK + wartość
   brak    → ODPOWIEDŹ_HUBA UNAVAILABLE   + zgłoś CORE „ktoś tego chce"
```

Drugi wiersz jest ważny: `UNAVAILABLE` wychodzi **natychmiast**, a dopiero potem CORE sięga
po wartość w tle ([`04`](../lagunbus-spec/04-interakcje.md) §8.2). Zapytanie jest sygnałem
popytu, nie przelotem na zewnątrz instalacji.

### 4.5 Czego ścieżce szybkiej nie wolno

Lista jest krótsza niż w poprzedniej wersji tego dokumentu, bo budżet jest luźniejszy (§5).
Zostaje w niej to, co chroni przed **zatrzymaniem na nieokreślony czas**, a nie to, co
oszczędza mikrosekundy:

| Zakaz | Dlaczego |
|---|---|
| **żadnego I/O poza `send()`** | plik, baza, sieć — każde z nich potrafi stanąć na sekundy |
| **żadnej mutacji modelu** | model należy do CORE; RX widzi wyłącznie snapshot |
| **żadnego czekania na lock trzymany przez inny wątek** | krótki lock lokalny jest w porządku, lock współdzielony z CORE nie |
| **`put_nowait`, nigdy `put`** | kolejka pełna znaczy, że CORE stoi; RX nie ma prawa stanąć razem z nim |

Logowanie na ścieżce szybkiej jest **dozwolone, ale niepotrzebne** — ramka i tak trafia do
kolejki wolnej, gdzie CORE może ją zapisać z pełnym kontekstem.

Ostatni wiersz wymaga rozstrzygnięcia, co zrobić z ramką, gdy kolejka jest pełna:
**porzucić ramkę wolną i podbić licznik**, nigdy nie blokować RX. Jest to ta sama zasada,
którą węzeł stosuje przy przepełnieniu kolejki nadawczej
([`types/0x0002-diagnostics.md`](../lagunbus-spec/types/0x0002-diagnostics.md) §5.3) — ginie
raport, nigdy zdarzenie.

---

## 5. Ile czasu mamy naprawdę

### 5.1 Budżet

| | Wartość | Skąd |
|---|---|---|
| **Cel projektowy** | kilka ms od zdarzenia do komendy | tyle wychodzi naturalnie z §4 |
| **Dopuszczalne** | **do ~50 ms** | poniżej progu, na którym człowiek zauważa zwłokę |
| **Do zbadania** | powyżej 100 ms | przełącznik zaczyna sprawiać wrażenie zepsutego |

Nie gonimy za milisekundą. Architektura z §3 daje kilkadziesiąt mikrosekund sama z siebie
i to wystarcza z zapasem trzech rzędów wielkości — ale **zapas jest po to, żeby go zużywać
na czytelność kodu**, a nie po to, żeby go bronić.

> **Praktyczny wniosek:** jeśli kiedykolwiek stanie wybór „prościej albo szybciej",
> wybieramy prościej. Wracamy do tego dopiero, gdy pomiar pokaże liczby powyżej 50 ms.

### 5.2 Co z tego zostaje w mocy

Luźny budżet nie unieważnia podziału na dwie ścieżki, bo ten nigdy nie był o mikrosekundach.
Chroni przed czymś innym — przed **zatrzymaniem bez końca**:

| Zagrożenie | Ile potrafi trwać | Czy podział pomaga |
|---|---|---|
| ponowne łączenie z brokerem MQTT | sekundy do minut | **tak** — inny wątek |
| `fsync` na karcie SD w RPi | dziesiątki do setek ms | **tak** — wątek STORE |
| enumeracja nowego węzła (34 transakcje) | ~2 s przy powtórzeniach | **tak** — CORE |
| przebudowa snapshotu przy 100 węzłach | kilka ms | bez znaczenia |
| przełączenie wątków przez GIL | do 5 ms domyślnie | bez znaczenia przy budżecie 50 ms |
| przebieg śmieciarki | jednostki ms | bez znaczenia |

Trzy pierwsze wiersze są powodem, dla którego ta architektura ma sens. Trzy ostatnie były
w poprzedniej wersji dokumentu opisane jako zagrożenia i **nimi nie są** — przy budżecie
50 ms mieszczą się w szumie.

Zostaje z nich jedna linijka, którą i tak warto napisać, bo nic nie kosztuje:

```python
sys.setswitchinterval(0.001)  # szybsze oddawanie GIL; nie jest to warunek poprawności
```

`gc.freeze()`, `SCHED_FIFO`, przypinanie do rdzeni i unikanie alokacji — **wypadają**.
Wracamy do nich tylko wtedy, gdy pomiar da powód, a nie zawczasu.

### 5.3 Snapshot zamiast locka

Zostaje mimo poluzowania budżetu, bo **nie jest optymalizacją** — jest sposobem na to, żeby
RX nie czekał na CORE w ogóle.

```
CORE                                     RX
  buduje nowy Snapshot (frozen)
  hub.snapshot = nowy          ─────►    snap = hub.snapshot     ← jedna referencja
                                         snap.addr[…]            ← czyta spójny obraz
```

Podstawienie referencji jest w CPythonie niepodzielne, a RX czyta ją **raz na ramkę** do
zmiennej lokalnej. Nie ma stanu pośredniego: RX widzi albo stary snapshot w całości, albo
nowy w całości. Locka nie ma w ogóle — i nie ma czego debugować, gdy zacznie się zacinać.

### 5.4 Agreement with the specification

The specification adopted the same budget: [`07-hub.md`](../lagunbus-spec/07-hub.md) §4.2
requires at most 50 ms from receiving an event to handing the command to the bus, with no
queuing of bindings. It used to say "below 1 ms" and the hub knowingly diverged from it —
that divergence is closed.

---

## 6. Model instalacji

Model jest w **całości w pamięci**. Baza jest kopią zapasową i miejscem startu, nigdy
źródłem odpowiedzi w czasie pracy.

Today the starting point is the configuration file, not a database: each joined node's
entry carries `joined` — address, bus, `ShortId` — and the model starts from those, with
the descriptor read again at the node's first frame (§8.6). `core/store/` (SQLite) is
written and tested but `lagun-hub run` does not open it (`zadania/FEAT-07.md`).

```
Installation
 │
 ├── nodes: dict[Uid, Node]                   ← klucz to Uid, NIGDY adres (07-hub §2)
 │     │
 │     └── Node
 │          ├── uid, short_id
 │          ├── address         ← zmienny; to jest droga, nie tożsamość
 │          ├── bus             ← jedyne miejsce w systemie, gdzie ta wiedza istnieje
 │          ├── vendor_id, device_type, firmware, hardware
 │          ├── state           UNKNOWN PENDING JOINED READY ABSENT RETIRED
 │          ├── endpoints: dict[int, Endpoint]
 │          │     └── Endpoint
 │          │          ├── type_id, revision, features, channel
 │          │          ├── enabled, report_interval, query_enabled
 │          │          └── attributes: dict[int, Value]    ← ostatni znany stan
 │          ├── names: dict[channel|endpoint, str]         ← nadane przez człowieka
 │          └── last_heard, silence_threshold
 │
 ├── addresses: AddressPool                   ← bloki wg NodeClass (03-adresacja §2.3)
 ├── bindings: list[Binding]
 └── groups: dict[int, set[(Uid, endpoint)]]
```

Z tego CORE wylicza snapshot dla ścieżki szybkiej:

```
Snapshot (frozen, podmieniany w całości)
 ├── addr:    dict[int, Uid]                          adres → tożsamość
 ├── bind:    dict[(Uid, ep, elem), tuple[Target,…]]  źródło → prekompilowane ramki
 ├── inputs:  dict[(Uid, ep, elem), bytes]            gotowe odpowiedzi na ZAPYTANIE
 └── sockets: tuple[socket, …]                        indeksowane numerem magistrali
```

Snapshot jest **wyłącznie tym, czego potrzebuje ścieżka szybka**. Wszystko inne — nazwy,
wersje firmware, historia — zostaje w modelu i RX tego nie widzi.

### 6.1 Uid, nie adres

Zasada z [`07-hub.md`](../lagunbus-spec/07-hub.md) §2 jest w kodzie egzekwowana typem:
`Uid` jest osobnym typem (`NewType`), a adres zwykłym `int`. Funkcja przyjmująca `Uid`
nie przyjmie adresu i odwrotnie. Jedynym miejscem, w którym oba występują naraz, jest
`Snapshot.addr` i odwrotne odwzorowanie w modelu — czyli **dokładnie te dwa miejsca, które
trzeba odbudować po przeadresowaniu**.

---

## 7. Wątki

Pięć, plus wątek klienta MQTT, którym zarządza biblioteka.

| Wątek | Blokuje na | Odpowiada za | Czego mu nie wolno |
|---|---|---|---|
| **RX** | `select()` nad socketami | dekodowanie, ścieżka szybka, rozdział reszty | jakiejkolwiek blokady poza magistralą |
| **CORE** | `queue.get(timeout=…)` | model, transakcje, timery, enumeracja, przyłączanie, obecność | blokującego I/O — od tego są STORE i NORTH |
| **STORE** | własna kolejka | zapis do SQLite z opóźnieniem — **not started today** (§6) | odpowiadania komukolwiek w czasie rzeczywistym |
| **ADAPTER** | gniazdo HTTP albo kolejka | tłumaczenie modelu na API / MQTT i komend z powrotem | dotykania modelu inaczej niż przez kolejkę CORE (§10.7) |
| **BUSWATCH** | `Event.wait(1 s)` | looking at each SocketCAN interface, bringing a failed one back up (§9.3) | touching the model other than through CORE's queue; running `ip` on CORE's behalf |

**No hub thread ends on a failing bus** (`zadania/zrobione/BUG-04.md`). An `OSError` from one bus is
counted against that bus — in RX on `recv`, by the sender on `send` — and the thread goes
on with the others. A thread that ends anyway, for another reason, is said once on stderr
and in the issues (`cli._dead_threads`), and the API answers at once that CORE has stopped
instead of waiting out its timeout.

### 7.1 Dlaczego nie ma wątku TX

Bo nie jest potrzebny. `send()` pojedynczej ramki na surowym sockecie CAN jest niepodzielny
— jądro przyjmuje ją w całości albo odrzuca. Dwa wątki nadające jednocześnie nie mogą
wytworzyć ramki zniekształconej, a kolejność i tak rozstrzyga arbitraż na magistrali,
nie kolejność wywołań w Pythonie.

Wątek TX dołożyłby więc kolejkę i przełączenie kontekstu w zamian za porządek, którego nikt
nie potrzebuje.

Zostaje jeden przypadek do obsłużenia: kolejka nadawcza jądra pełna (`ENOBUFS`). Socket jest
nieblokujący, więc `send()` zgłasza wyjątek zamiast czekać.

| Kto nadaje | Co robi przy `ENOBUFS` |
|---|---|
| ścieżka szybka | porzuca, podbija licznik, **nie powtarza** — komenda spóźniona jest gorsza niż brak |
| CORE | oddaje żądanie własnemu timerowi; powtórzenie i tak jest w transakcji |

The same for a bus that is down (`ENETDOWN` on every `send` while the interface is down):
both senders catch any `OSError`, not only `ENOBUFS`, and count the frame as dropped. Before
`zadania/zrobione/BUG-04.md` that error ended the thread — RX on a binding's command, CORE on a
retry, and with CORE the API.

### 7.2 Timery bez wątku timera

CORE czeka na kolejce **z limitem czasu wyliczonym z najbliższego terminu**:

```
while running:
    deadline = timers.next_deadline()
    try:
        work = queue.get(timeout = deadline - now())
    except Empty:
        work = None
    timers.fire_due()
    if work: handle(work)
```

Jeden wątek obsługuje wtedy powtórzenia transakcji (100 ms), porzucanie segmentów (100 ms),
okno deduplikacji, wygaszanie trybu przyjmowania (15 min) i wykrywanie nieobecności.
Osobny wątek timera byłby drugim zegarem obok pierwszego.

### 7.3 Nieobecność

Próg nie jest stałą: **dwa razy najkrótszy niezerowy `ReportInterval` wśród endpointów
węzła**, a 300 s tylko wtedy, gdy raportowanie cykliczne wyłączono wszędzie
([`06`](../lagunbus-spec/06-cykl-zycia-urzadzenia.md) §5.1). Wylicza się go raz, po
enumeracji, i trzyma w `Node.silence_threshold`. Każda ramka od węzła — dowolnego rodzaju —
przesuwa `last_heard` i to wszystko, co ścieżka wolna musi z tym zrobić.

Po przekroczeniu progu CORE odczytuje `Device.Uptime`. Trzy nieudane próby w odstępach 10 s
oznaczają `ABSENT`.

---

## 8. Przyłączanie i adresacja

Jedyna procedura, w której hub nadaje ramkę o **innym układzie identyfikatora**
([`05`](../lagunbus-spec/05-mapowanie-can.md) §4).

```
ZGŁOSZENIE (K=1, ShortId w ID)
  ładunek: ShortId 32 b, NodeClass
        │
        ▼  CORE porównuje PEŁNE 32 bity ShortId, nie tylko te z identyfikatora
   ┌────────────────────────────────────────────┐
   │ Uid zablokowany w pliku?  → nic, BLOCKED   │  także przy włączonym trybie (§8.4)
   │ ShortId znany?                             │
   │   TAK  → adres, który ten węzeł miał       │  zawsze, bez pytania o tryb
   │   NIE  → tryb przyjmowania włączony?       │
   │            TAK → pierwszy wolny z BLOKU    │  blok wg NodeClass
   │            NIE → zapisz jako PENDING       │  nie przydzielaj
   └────────────────────────────────────────────┘
        │
   PRZYDZIAŁ (K=0, ShortId w ID) ── ładunek: ShortId, adres u16
        │
   Booted spod nowego adresu  →  wpis potwierdzony  →  enumeracja
```

Asymetria z [`06`](../lagunbus-spec/06-cykl-zycia-urzadzenia.md) §2.1 jest tutaj cała:
**znane urządzenie dostaje swój adres zawsze**, bo wymiana bezpiecznika nie ma wymagać
ceremonii. Tryb chroni wyłącznie przed wejściem urządzenia nieznanego i wygasa sam po
piętnastu minutach.

The address pool keeps three blocks (`CONTROL` `0x001`–`0x0FF`, `SENSOR` `0x100`–`0x3FF`,
reserve `0x400`–`0x7FD`). An address a removed node may still speak from is protected by
the pool's *heard* set, not by a state of its own (§8.4).

### 8.3 A node at an address the hub does not know

A node with an address never announces itself, so joining will not bring it in when the
hub's database lacks it — a lost or old database, a node removed by force whose
`FactoryReset` did not reach it, a module used elsewhere ([`06`](../lagunbus-spec/06-cykl-zycia-urzadzenia.md)
§2.3). `core/strangers.py` handles it:

```
any frame FROM a node (report, event, alarm, reply, query)
   │
   ├─ address → AddressPool.hear()          heard = taken, whatever the database says
   │
   └─ nobody holds it in the database?
         read Device.Uid, then Device.ShortId      once per address, 60 s pause on silence
            Uid known       → issue: database and bus disagree
            ShortId known   → issue: the same, seen from the other key
            Uid blocked     → JoinRecord BLOCKED with that address (§8.4)
            Uid in the file → JOINED, enumeration, READY, `joined` written    no operator
            otherwise       → JoinRecord PENDING with that address
                                 admit          → JOINED, enumeration, READY
                                 factory_reset  → the node forgets the address and joins anew
```

**Every frame is the trigger, not `Booted`**: the hub on a Raspberry Pi comes up minutes
after the nodes, and their `Booted` is gone by then. The mandatory `Diagnostics` reports
bring every stranger within five minutes, without polling (07-hub.md §5.1).

**A pending node with an address is never admitted by itself**, not even with the
accepting mode open — the mode is for nodes that announce, i.e. nodes without one.
**A node whose `Uid` is in the file is not pending**: the entry is the operator's
decision, made before, so it is enumerated straight away — the case of a file older than
the bus, such as a backup or a node joined before the hub wrote addresses down
(`zadania/zrobione/BUG-05.md`, D4; `lagunbus-spec/DECYZJE.md`, "Węzeł z adresem, którego `Uid` hub
ma w konfiguracji, wchodzi sam").

**Heard addresses live in memory only.** After a restart every working node is heard again
within one reporting period; an address of a node gone for good stays blocked until the
next restart, which out of two thousand costs nothing.

Two nodes under one address on **one** cable stay invisible — an accepted risk, prevented
by the rule that a used module is reset to factory settings before it is plugged in
(`lagunbus-spec/DECYZJE.md`, "Konflikt adresów: zapobieganie zamiast wykrywania").

### 8.4 Removing, blocking, reading again

Three operator operations from [`06`](../lagunbus-spec/06-cykl-zycia-urzadzenia.md) §6,
§8 and §3.3, worked out in `zadania/zrobione/FEAT-04.md`.

```
remove(node)          FactoryReset, wait ── acknowledged ─→ forget: model, join record,
                                         └─ silent/refused → error, nothing changes
remove(node, force)   FactoryReset, do not wait ──────────→ forget
block(node)           nodes.yaml: blocked: true ──────────→ BLOCKED on reload
unblock(node)         the field goes ─────────────────────→ enumeration → READY
enumerate(node)       read identity and descriptor again; the old model stays until then
```

**Removing forgets everything**: the model, every join record naming the node, its entry
in `nodes.yaml` and every binding it takes part in — a binding naming an alias nobody
holds would make the whole file fail to load. The file is changed by
`ConfigStore.forget_node`, the one write that touches two files; bindings are written
first, so a crash in between leaves a node without bindings (loads), never bindings
without their node (does not). There used to be a `RETIRED` state for this; it kept a
note nothing acted on, and a forgotten record behaves the same, including for a node the
reset never reached (`lagunbus-spec/DECYZJE.md`, "Usunięcie: zapomnienie w dwóch trybach;
zablokowanie osobno").

**The address after removal.** It goes back to the pool at once, but stays *heard* until
the node acknowledges its reset — then `Strangers.forget` lets go of it, and the same node
admitted again usually gets the same address. A forced removal that is acknowledged late
frees it the same way; one never acknowledged keeps it blocked until the hub restarts, and
the module, if it still works, comes back as `PENDING` (§8.3).

**Blocking lives in the file**, keyed by `Uid`: it is an operator's decision, it has to
survive a restart, and an address changes when a node is admitted again. A
blocked node is recognised wherever its `Uid` is known — in a join record, after
enumeration, or by `Strangers` after a restart — and then gets no address (accepting
mode or not), is not enumerated, reconciled or controlled through the API, and keeps its
address. Bindings are not checked on the fast path: `Installation.snapshot()` leaves out
every binding whose source or target is blocked, so RX never sees them. One thing
blocking cannot take back: **group membership already written to the node** stays in
it, so a group command still reaches it. Undoing that means talking to the node, which a
block exists not to do.

**Enumerating again** replaces the model only when the new one is complete; a failure
leaves the node working as it was and is reported as an issue and a `NODE` event — before
this, a failed enumeration dropped its reason.

### 8.5 Reading everything

`read_all(node[, endpoint])` reads every readable attribute the node — or one endpoint —
has, on an operator's request (`G` in the view), worked out in `zadania/zrobione/FEAT-06.md`.
Before it the hub knew a value only when the node reported it, reconciling read it, or
somebody pressed `g` on one attribute; a relay with four channels is about 170 of them.

```
read_all(node)        Readout: one READ after another ── each reply → model (CORE)
                       └─ returns at once {started, pending}; node().reading counts down
                       └─ end: NODE event READOUT_DONE "69 read, 12 absent"
```

- **Not polling.** It runs only when a person asks, like `g`; the hub still listens
  rather than asks after a restart, and a whole-installation readout is deliberately
  not offered.
- **What is not asked:** attributes needing a feature the endpoint did not declare,
  optional ones the node already turned down (both `EndpointModel.lacks`), and endpoints
  of a type the hub does not know. Long values (`string`, `blob`) go last.
- **One after another per node**, like reconciling; several nodes may be read at once.
  One silence stops it — three attempts already went out.
- **Refusals.** An optional attribute refused with `UNSUPPORTED_ELEMENT` is remembered in
  `absent` by `CoreThread._remembering` — for every read, `g` and reconciling included,
  not by each caller. A *mandatory* one refused is the node failing its type, is not put
  in `absent` (the API would then claim the device sets it itself), and goes into **one**
  issue per readout with every other refusal: the node library lacks several common
  attributes today (`zadania/TASK-01.md`), and an issue each would bury the list.
- **Stopped by** removing, blocking or enumerating the node — enumeration builds a new
  model, whoever started it (`Enumerator`'s `on_start`). Refused with `CONFLICT` while
  the node is being enumerated or already read; alongside reconciling it is allowed.

### 8.6 After a restart

Worked out in `zadania/zrobione/BUG-05.md`. Before it a restart looked to the installation like a
lost database: every node came back without an address, every operation ended at "the
hub has not seen it yet", and a node that spoke waited as `PENDING`.

```
joining ends (enumeration READY)   →  nodes.yaml:  joined: {address, bus, short_id}
                                        written only when it differs; ConfigStore.set_joined
start, `apply`                     →  each entry with `joined`: NodeModel READY,
                                        address reserved, join record under ShortId,
                                        described = False — no frame, no polling
first frame from such a node       →  enumeration → reconciling          (CoreThread._first_frame)
an operation before that frame     →  enumeration of that node, then the operation  (HubApi._described)
a factory-reset node announcing    →  its ShortId is known → its old address, no mode
```

- **In the file means joined.** Not "not heard since the start", not `ABSENT`, not polled:
  bindings with the node as a target work from the first second (the snapshot needs an
  address, nothing else), and so do commands. The rule is the user's: "if it is in
  nodes, we know the device, it is joined, we talk to it directly".
- **Why the file and not the database.** Of what the database would hold, only the
  address and `ShortId` cannot be read back at will — the hub assigned the address, a
  silent node does not say it, and an address forgotten while its node is off would go
  to another node. The descriptor and firmware are read again at the first frame, about
  35 ms per node. Nothing is *keyed* by address: the key stays `Uid`, so a renumbering
  changes one field and breaks nothing (KONFIGURACJA.md §12).
- **The hub writes the person's file.** Only `joined`, only from CORE, with the current
  digest (like `blocked`), comments kept. A write that carries along a hand edit not yet
  picked up goes in as a reload does, reconciling included; a write that fails is an
  issue, the node keeps working. Removing a node removes its whole entry (§8.4), so
  `joined` goes with it.
- **The bus** is named as in `hub.yaml`, or as the open bus is named when the file has
  no such bus (`udp` on a trial). Read back by that name; with one open bus, that one
  whatever the name; with several and no match, not loaded — an issue, and the node is
  found at its first frame (§8.3).
- **The model wins over the file.** `apply` loads `joined` only for a node the model
  does not hold; two entries claiming one address are an issue, not a pick.
- **Not done:** a readdress (`Installation.readdress`) has no caller yet; whoever adds
  one calls `CoreThread.note_joined` too.

### 8.2 Dlaczego model ma dwie tablice

Między przydziałem adresu a enumeracją hub jest w stanie, który łatwo przeoczyć przy
projektowaniu: **zna `ShortId` i adres, ale nie zna jeszcze `Uid`** — a `Uid` jest kluczem
modelu instalacji.

```
 ZGŁOSZENIE  ──►  JoinRecord      klucz: ShortId   ← stan przyłączania (06 §1.2)
                       │
                  enumeracja      odczyt Device.Uid
                       ▼
                  NodeModel       klucz: Uid       ← model instalacji (07-hub §2)
```

Kusi, żeby do czasu enumeracji wstawić `Uid` wyprowadzony z `ShortId`. Byłoby to
jednak **drugą tożsamością mającą jak rozjechać się z prawdziwą** — dokładnie tym,
przed czym broni reguła, że tożsamością jest `Uid` i nic innego. Dwie tablice są tańsze
niż jedna z kluczem, który czasem kłamie.

### 8.1 Enumeracja wymaga segmentów

Kolejność zależności warta odnotowania, bo łatwo o nią potknąć się w planie:
`Device.Uid` jest typu `blob` o długości 8–16 bajtów, więc **nie mieści się w ramce**
i przyjeżdża w segmentach ([`05`](../lagunbus-spec/05-mapowanie-can.md) §6).

> **Bez reasemblera segmentów hub nie odczyta tożsamości ani jednego węzła.**

Reasembler trzyma stan **per węzeł**, bo węzeł składa najwyżej jedną wartość w każdą
stronę naraz (§6.7): bufor, oczekiwany numer segmentu, termin 100 ms. Segment `0`
unieważnia wszystko, co zebrano wcześniej dla tej wartości (§6.5), dziura w numeracji
porzuca wartość w całości, retransmisji częściowej nie ma.

Dla warstwy transakcji zmienia się przy tym **wyłącznie liczba ramek**: `SEGMENTED`
jest zapowiedzią, nie wynikiem, więc transakcja trwa dalej, a jej czasu pilnuje od tej
chwili reasembler. Utrata segmentów powtarza **całe żądanie**, w ramach tych samych
trzech prób.

---

## 9. Warstwa magistrali

### 9.1 Surowy socket

```python
sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
sock.bind(("can0",))
sock.setblocking(False)
```

Ramka to 16 bajtów: `<IB3x8s` — identyfikator (z flagą `CAN_EFF_FLAG`), długość, wypełnienie,
dane. Dekodowanie identyfikatora to cztery przesunięcia i maski:

```
 28 27 26 25 │ 24 … 14 │ 13 … 8 │ 7 … 0
   RODZAJ    │  ADRES  │   EP   │ ELEM
     (4)     │   (11)  │   (6)  │  (8)

kind     = (can_id >> 25) & 0x0F
address  = (can_id >> 14) & 0x7FF
endpoint = (can_id >>  8) & 0x3F
element  =  can_id        & 0xFF
```

**Hub przepuszcza wszystko** ([`05`](../lagunbus-spec/05-mapowanie-can.md) §3), więc filtrów
sprzętowych nie ustawiamy — poza jednym zastosowaniem diagnostycznym, gdzie chcemy podglądać
wybrany węzeł bez reszty ruchu.

### 9.2 Transport wymienny

Rozwój dzieje się na macOS, gdzie SocketCAN nie istnieje. Dlatego transport jest interfejsem
od pierwszego dnia, a nie po pierwszym bólu:

```python
class Bus(Protocol):
    name: str

    def fileno(self) -> int: ...  # do select()
    def recv(self) -> Frame | None: ...
    def send(self, frame: Frame) -> None: ...
```

| Implementacja | Gdzie działa | Do czego |
|---|---|---|
| `SocketCanBus` | Linux, `can0` / `vcan0` | RPi i docelowa instalacja |
| `VirtualBus` | wszędzie, w jednym procesie | testy, atrapa węzła ([`tests/node/`](tests/node/)) |
| `UdpBus` | wszędzie, między procesami | **test only**: `lagun-hub run --udp` next to `lagun-sim` — the only way to run the hub on macOS. Never a kind of bus in `hub.yaml`. No arbitration between processes. Format: `../lagunbus-node/README.md` § Decisions |
| `ReplayBus` | wszędzie | odtwarzanie zapisanego ruchu, testy regresji |

`VirtualBus` udaje **arbitraż**: przy dwóch ramkach gotowych jednocześnie przepuszcza tę
o niższym identyfikatorze. Bez tego testy priorytetów nie znaczyłyby nic.

### 9.3 A SocketCAN bus that goes down

What the kernel does to a CAN_RAW socket: as the interface goes `down`, `recv` fails
**once** with `ENETDOWN`; while it is down, every `send` fails with `ENETDOWN`; after `up`
the same socket works again, because it is bound to the interface's *index*, not its state.
An interface that goes away (a USB adapter pulled out) fails `recv` once with `ENODEV`, and
comes back — if it does — under a new index the old socket never hears.

**Who took it down decides** (`zadania/zrobione/BUG-04.md`). The hub takes an interface down only
when it reconfigures it (`bus_apply`, "apply" in the `Buses` view); it tells the watch
first (`BusWatch.planned`) and that break is not reported. Any other way down — the
controller's driver, `ip link` in another terminal, the adapter pulled out, the interface
down at start — is a **failure**: reported at once, and brought back up by the hub with the
same `buses.bring_up` the start uses. Three attempts (1 s, 5 s, 30 s); none when sudo wants
a password; more than three failures in ten minutes and the hub stops and says it is the
hardware. Having given up it still looks, and a bus brought up by hand, or an interface
that comes back, is picked up without a restart.

| piece | does |
|---|---|
| `RxThread` | survives the `OSError`, counts it (`rx.errors`); registers each bus's **current** descriptor with `select` on every turn — by number, not by the bus object |
| `SocketCanBus` | `missing_ok` at start: no interface yet gives an unbound socket instead of an error, so the bus is still the hub's; `reopen()` swaps the socket in place — RX, the engine and CORE each hold the bus, not the socket |
| `BusWatch` (`lagunhub/buswatch.py`) | once a second reads `flags` and `ifindex` in `/sys/class/net/<channel>` — no process, no privileges; up, down, gone, or back under a new index. Repairs and reopens on its own thread: `sudo ip` can take seconds and CORE answers nodes in milliseconds. Reports go into the issues through CORE's queue |
| `BusView.link` / `BusSetupView.link` | `up`, `planned`, `failure`, `gave-up` (`fault` says why) — in `health`, `/buses` and the view |

The watch does not rely on RX seeing the error: RX sees `ENETDOWN` only if it was waiting
on the socket at that moment, and never sees an interface that was down before the hub
started. Only SocketCAN is watched; other kinds of bus only "do not die".

---

## 10. Adaptery

Poza zakresem specyfikacji ([`07-hub.md`](../lagunbus-spec/07-hub.md) §7), więc siedzą za
interfejsem i da się je wymieniać. Będą dwa — i **pierwszy jest API, nie MQTT**.

### 10.1 Rdzeń i adaptery

Docelowo hub dzieli się na **rdzeń** — czystą logikę bez wiedzy o świecie zewnętrznym —
i **adaptery**, z których każdy tłumaczy ten sam zestaw operacji na inny sposób rozmowy.

```
                     ┌──────────────────────────────┐
                     │            RDZEŃ             │
                     │  magistrale · model · RX     │
                     │  powiązania · konfiguracja   │
                     └──────────────┬───────────────┘
                                    │  API rdzenia
                     ┌──────────────┴───────────────┐
                     │                              │
              ┌──────▼──────┐  ┌──────▼──────┐  ┌───▼─────────┐
              │  adapter    │  │  adapter    │  │  adapter    │
              │    TUI      │  │    REST     │  │    MQTT     │
              └─────────────┘  └─────────────┘  └─────────────┘
```

> **Wartościowa jest nazwana lista operacji, nie układ katalogów.** Przeniesienie
> plików do `core/` i `adapters/` daje ładniejsze drzewo i dokładnie to samo sprzężenie.
> Granica zaczyna istnieć w chwili, gdy powstaje `api.py` z operacjami — resztę da się
> przestawić w dowolnym momencie.

Dlatego kolejność jest taka: **najpierw API rdzenia razem z pierwszym adapterem**,
potem drugi adapter, a podział pakietów **dopiero po drugim** — wtedy granicę się
odkrywa, a nie zgaduje. Pakiety przestawione po pierwszym adapterze trzeba by
przestawiać drugi raz.

### 10.2 Pierwszy był klient w terminalu

Pierwszym adapterem była powłoka `lagun-hub shell` — bo nie potrzebowała ani brokera, ani
przeglądarki, a przede wszystkim **wymusiła API, zanim powstał transport**: pytania „jak
nazwać operację" i „co zwraca" padły raz, a nie przy trzecim adapterze. Z tamtego czasu
zostały dwie reguły, które obowiązują dalej:

- **hub jest programem serwerowym, a klient w terminalu jest jego klientem** — dołącza
  się do chodzącego huba przez HTTP i nigdy nie startuje własnego (dwa huby na jednej
  magistrali to dwa przydziały adresów i dwie prawdy),
- **operacje zwracają dane, nie napisy** — `HubClient` oddaje te same zamrożone
  dataclassy, które wystawia `api/views.py`, a tekst robi z nich dopiero widok.

### 10.3 Powłoki nie ma — w terminalu jest widok pełnoekranowy

Do 2026-10 nad tym samym klientem stały dwa interfejsy: powłoka (REPL z uzupełnianiem)
i widok pełnoekranowy (§10.3.3). Powłoka została usunięta
([`CR-06`](../zadania/zrobione/CR-06.md)). Widok robi każdą operację, którą miała,
a utrzymywanie dwóch klientów kosztowało przy każdej zmianie API drugie poprawki, drugie
testy i drugą dokumentację.

**Odrzucona alternatywa: zostawić obie.** Powłoka miała jedną przewagę, której widok nie
powtarza — tabulator nad żywą instalacją. Widok jej nie potrzebuje, bo wartości nazwane
**wybiera się z listy** (`choices` z `EndpointView`), a nie wpisuje. Strumień zdarzeń
(`watch`, `feed`) też nie przepadł: dolny panel widoku pokazuje ramki, a stan widać
w listach (§10.3.3). `GET /events` zostaje w API — dla `curl` i dla MQTT.

```
lagunhub/adapters/client/
└── client.py      HubClient (HTTP + SSE), discover(), protokół Hub  ← jedyne miejsce z transportem
```

`Hub` to protokół, który spełniają i `HubApi`, i `HubClient` — dlatego widok testuje się
na atrapie (`tests/fake_hub.py`), a `tests/test_client.py` pilnuje, żeby obie strony się
nie rozjechały.

**Czego klient w terminalu nie załatwi.** *Automatyzacji* — do skryptów i CI jest REST
(`curl`, przykłady w [`README.md`](README.md)). *Huba, który nie odpowiada* — klient nie ma
wtedy z czym rozmawiać; diagnostyka schodzi poziom niżej, do `lagun-hub monitor`, który
słucha magistrali i huba nie potrzebuje wcale. **Rozruch na Raspberry Pi przez SSH** tego
nie wymaga: `lagun-hub run --accept-new`, `monitor`, widok (`curses` jest
w bibliotece standardowej i działa w każdym terminalu SSH) i `curl`.

### 10.3.3 Widok pełnoekranowy

Podział ekranu — u góry nawigacja, na dole ruch na magistrali — jest naturalnym
następnym krokiem i **wolno w nim poszaleć właśnie dlatego, że to adapter**. Cokolwiek
się w nim wydarzy, rdzeń widzi wyłącznie `api.py`.

```
┌─ LagunBus ──────────────────────────── can0 ● can1 ● ── 12:41:07 ─┐
│  ▸ Węzły          UID               ADR    STAN      SŁYSZANY     │
│    Powiązania     0001020304050607  0x02A  GOTOWY        0.4 s    │
│    Konfiguracja  ▸08090a0b0c0d0e0f  0x040  GOTOWY        1.2 s    │
│    Kondycja       1a2b3c4d5e6f7081  0x150  NIEOBECNY   11 min     │
│    Usterki   (2)                                                  │
│                   ── Lampa nad stołem · can0 ──────────────────   │
│                   ep3  Scenes                      0/8 scen       │
│                   ep4  Output  LIGHT  ch1          OnOff=false    │
│                   [t] Toggle  [i] Identify  [e] Edytuj            │
├─ Ruch na magistrali ──────────── 42 ramki/s ── [spacja] pauza ────┤
│ [  61.204] can0  EVENT     0x02A ep4 Click seq=17                 │
│ [  61.205] can0  COMMAND   0x040 ep4 Toggle tid=1                 │
│ [  61.208] can0  RESPONSE  0x040 ep4 OK tid=1            3.1 ms   │
│ [  61.210] can0  REPORT    0x040 ep4 OnOff=false                  │
└───────────────────────────────────────────────────────────────────┘
 ↑↓ wybór  ⏎ szczegóły  / filtr  q wyjście    p99 22µs · kolejka 0
```

Makieta wyżej jest pierwotna; kod ma etykiety po angielsku i sekcję `Drift` zamiast
`Konfiguracja` (pokazuje **różnicę**, a samą konfigurację widać przy atrybutach).
Trzy rzeczy, które trzeba było rozstrzygnąć, zanim to powstało — wszystkie są w adapterze:

**Zależność jest opcjonalna — a wyszło lepiej: nie ma jej wcale.** Wymaganie było takie,
żeby biblioteka pełnoekranowa nie weszła do zależności rdzenia. Rozstrzygnięte przy pisaniu widoku
na rzecz **`curses` z biblioteki standardowej**: `Textual` i `urwid` dają ładniejszy
wynik mniejszym kodem, ale obie ciągną własną pętlę `asyncio`, którą trzeba by mostkować
do klienta blokującego na gnieździe — ten sam argument, który rozstrzygnął **H7** przy
serwerze. Rdzeń zostaje bez zależności poza `ruamel.yaml`, a dodatku nie trzeba
instalować.

**Przeciwciśnienie należy do widoku.** Przy 500 ramkach na sekundę żaden terminal nie
nadąży. Podgląd ma **własny bufor pierścieniowy, który porzuca najstarsze**, i odrysowuje
się ze stałą częstością — nigdy nie wolno mu spowolnić kolejki, z której czyta, bo wtedy
najwolniejszy widok staje się wąskim gardłem huba.

**Pętla zdarzeń widoku to nie wątek CORE.** Biblioteka pełnoekranowa zwykle prowadzi
własną pętlę `asyncio`; obowiązuje ją dokładnie ta sama umowa co każdy adapter — czyta
widok przez kolejkę CORE i tą samą kolejką odsyła komendy (§10.7). Nic więcej.

> **Widok powstał po powłoce, nie przed** — i dobrze: stanął na kliencie, który już
> był, więc był w dużej części gotowy. Zbudowany wcześniej byłby jedynym konsumentem
> klienta i wyznaczyłby jego kształt pod siebie.

**Szczegóły obok listy, nie w głąb.** Pierwsza wersja widoku schodziła w głąb: człowiek
wchodził w węzeł i tracił z oczu resztę instalacji, wchodził w endpoint i tracił z oczu
węzeł. Przy czterdziestu puszkach w rozdzielnicy to jest nie do użycia. Dlatego lewa
kolumna ma stałą szerokość i zawsze pokazuje wszystkie sekcje z licznikami, a prawa
dzieli się na listę i szczegóły wybranej pozycji. **Klawisze akcji są wypisane pod
szczegółami** i zmieniają się z tym, co wybrane — nie trzeba ich pamiętać.

Rozstrzygnięcia z pisania widoku:

| Co | Jak | Dlaczego |
|---|---|---|
| dolny panel | **ramki** (`GET /frames`), nie zdarzenia semantyczne | to one czynią z widoku narzędzie diagnostyczne; zdarzenia widać i tak w listach wyżej |
| sekcje i głębokość | sekcje na `⇥`, `← →` chodzi **w głąb** | strzałki w bok obok strzałek w górę i w dół muszą znaczyć „to samo, w drugiej osi" |
| uzgodnienie rozjazdu | `R`, nie `r` | `r` odświeża wszędzie; dwa znaczenia jednego klawisza to pułapka |
| reading everything | `G`, a capital `g`; inside an endpoint only that one | the same split as `g` against `R`: the key under the cursor's scope. Progress shows in the pane title, because the view polls rather than listens (§14.5) |
| `p` — przyjmij wartość z węzła do pliku | **jest, ale tylko jawnie** | łamie zasadę „plik → węzeł, nigdy odwrotnie", więc wolno tylko człowiekowi; bez tego jedyną drogą jest ręczna edycja pliku |
| ręczne wyzwolenie powiązania | **nie ma** | nowa operacja w API, a w dzienniku trudna do odróżnienia od prawdziwego zdarzenia |
| komendy z argumentami w powiązaniu | tylko bezargumentowe | argumenty dokłada się w pliku |
| wąskie okno | najpierw skracają się nazwy sekcji, potem kolumny; za małe mówi o tym | każdy rozmiar okna da się narysować |
| kolor | **powtarza** to, co napisane słowem | nigdy nie niesie treści sam |

**Czego widok jeszcze nie pokazuje, bo hub tego nie liczy** — w tych miejscach stoi
kreska, **lepiej kreska niż zmyślona liczba**: kiedy węzeł był ostatnio słyszany (próg
ciszy przyjdzie z wykrywaniem nieobecności, §14), ile razy zadziałało dane powiązanie (`BindingEngine` liczy
sumy), numer wiersza w pliku konfiguracji (`ruamel` go zna, API nie wystawia) i histogram
w czasie (`Latency` trzyma próbki, nie szereg).

### 10.4 Dlaczego MQTT jest ostatni

| Powód | |
|---|---|
| **wymaga ustalonego modelu** | rozgłaszanie do HA każe rozstrzygnąć kanał → encja, `Purpose`, cechy, kształt tematów; REST i widok nie wymagają niczego poza `api.py` |
| **najtrudniej sprawdzić, czy działa** | trzeba brokera, klienta HA i czytania cudzych tematów; `curl` i widok mówią prawdę od razu |
| **nie zastępuje pozostałych** | zostają jako kanał obsługowy i diagnostyczny, także wtedy, gdy broker leży |

Jest w tym jeszcze jeden zysk, mniej oczywisty: **dwa wcześniejsze adaptery zmuszają do
nazwania modelu, zanim zostanie on przetłumaczony na cudze pojęcia.** Encja Home Assistanta
jest interpretacją kanału; dobrze mieć wcześniej dwa miejsca, w których widać oryginał.

### 10.5 Interfejs

```python
class Adapter(Protocol):
    def on_state(self, uid, endpoint, attribute, value) -> None: ...
    def on_event(self, uid, endpoint, event, params) -> None: ...
    def on_model(self, installation) -> None: ...  # przy zmianie zestawu urządzeń
```

### 10.6 Kształt API

REST do odczytu i sterowania, SSE do strumienia zdarzeń.

```
GET  /nodes                                  lista: Uid, adres, stan, magistrala
GET  /nodes/{uid}                            model i stan wszystkich endpointów
GET  /nodes/{uid}/ep/{n}                     typ, cechy, kanał, atrybuty
GET  /nodes/{uid}/ep/{n}/config              konfiguracja POŻĄDANA obok FAKTYCZNEJ
POST /nodes/{uid}/ep/{n}/commands/{name}     sterowanie: Toggle, SetLevel, Identify…
PUT  /nodes/{uid}/ep/{n}/attributes/{name}   nastawa robocza (RW, V)
GET  /config                                 cała konfiguracja + ETag
PUT  /config/{ścieżka}                       zapis konfiguracji — If-Match, check-and-set
GET  /bindings                               powiązania z pliku, z rozwiązanymi Uid
GET  /issues                                 rozjazdy konfiguracji, konflikty adresów
GET  /health                                 histogram opóźnień, liczniki, ruch per magistrala (§11.1)
POST /commissioning/accept                   tryb przyjmowania, wygasa po 15 min
POST /commissioning/{ShortId}/admit         przyjmij węzeł PENDING spod jego adresu (§8.3)
POST /commissioning/{ShortId}/factory-reset reset fabryczny węzła PENDING (§8.3)
POST /nodes/{uid}/remove                     forget the node; {"force": true} does not wait (§8.4)
POST /nodes/{uid}/enumerate                  read the node again (§8.4)
POST /nodes/{uid}/read-all                   read every attribute, in the background (§8.5)
POST /nodes/{uid}/ep/{n}/read-all            the same for one endpoint (§8.5)
POST /nodes/{uid}/block                      blocked: true in the file (§8.4)
POST /nodes/{uid}/unblock                    the field goes; the node is read again (§8.4)
GET  /buses                                  buses from hub.yaml + the hardware under them: state, changes (FEAT-01)
GET  /buses/socketcan/{name}                 the same for one bus — paths per KIND, each kind its own contract
POST /buses/socketcan/{name}/apply           carry out what the hub may do itself; 502 when the hardware refuses
GET  /events                                 SSE — zdarzenia i zmiany stanu na żywo
```

Czwarty wiersz jest tym, dla którego warto to napisać wcześnie: **konfiguracja wraca
w dwóch wersjach naraz** — tak, jak stoi w pliku, i tak, jak faktycznie siedzi w węźle.
Rozjazd między nimi jest jedną z trudniejszych rzeczy do zdiagnozowania w takiej instalacji
i ma być widoczny wprost, a nie do wywnioskowania.

**API jest główną drogą konfigurowania huba** — aplikacja frontendowa stoi na nim, a edycja
pliku w edytorze jest drogą serwisową. Nie znaczy to, że zapis idzie prosto do węzła:

> **Zapis konfiguracji przez API zmienia plik, a nie węzeł.** Do węzła trafia dopiero przez
> uzgadnianie ([`KONFIGURACJA.md`](KONFIGURACJA.md) §11.1). Sterowanie — komendy i nastawy
> robocze — idzie wprost i pliku nie dotyka.

Jedna droga zamiast dwóch, więc plik nadal opisuje instalację, a nie to, co ktoś kiedyś
w nim napisał. Zapisy są **check-and-set po `ETag`**, bo plik ma dwóch piszących: aplikację
i człowieka ([`KONFIGURACJA.md`](KONFIGURACJA.md) §11.2).

Czego w tym API **nie ma i to jest celowe**:

| Brak | Dlaczego |
|---|---|
| adresowania po adresie węzła | adres jest drogą, nie tożsamością; API mówi `Uid` |
| pola „magistrala" jako czegoś, co się wybiera | hub sam wie, gdzie kogo widzi ([`03-adresacja.md`](../lagunbus-spec/03-adresacja.md) §2.1) |
| zapisu konfiguracji prosto do węzła | jedna droga, patrz wyżej |

**SSE, nie WebSocket.** Ruch jest jednokierunkowy — zdarzenia wychodzą, komendy i tak idą
`POST`-em. WebSocket dokładałby drugą ścieżkę do tego samego i drugi format wiadomości.
`zigbee2mqtt` używa tu WebSocketu, ale u niego frontend i tak rozmawia z brokerem przez
most; my nie mamy tej potrzeby. Gdyby kiedyś panel konfiguracyjny czegoś od WebSocketu
potrzebował, dołoży się go **obok** SSE, a nie zamiast.

### 10.7 Jak adapter dotyka modelu

Ta sama zasada co przy MQTT i ta sama co przy ścieżce szybkiej: **adapter nie dotyka modelu**.
Dwa mechanizmy, po jednym na kierunek:

```
  ADAPTER (wątek API)                          CORE
  GET  ──► queue.put(Task(build, future)) ──►  buduje widok z modelu
       ◄── future.result(timeout=2s)           (jest jego właścicielem)

  POST ──► queue.put(Command(…, future))  ──►  wykonuje, rozstrzyga future
       ◄── future.result(timeout=2s)

  PUT  ──► queue.put(ConfigEdit(patch, etag, future))
                                          ──►  waliduj → ZAPISZ PLIK → podmień
                                               → uzgodnij z węzłami w tle
       ◄── 200 + nowy ETag | 409 | 422
```

Kolejność w trzeciej linii jest istotna i opisuje ją
[`KONFIGURACJA.md`](KONFIGURACJA.md) §11.3: **plik zapisuje się przed podmianą stanu
w pamięci**, bo z dwóch możliwych awarii „zapisane, niezastosowane" naprawia się restartem,
a „zastosowane, niezapisane" znika po cichu przy najbliższym zaniku zasilania.

Pierwsza linia wyglądała tu inaczej: odczyt miał sięgać po **drugi** niezmienny widok
(`read_model`), publikowany przez CORE obok `Snapshot` przy każdej zmianie. Implementacja
API to odrzuciła, bo koszt szedł w złą stronę — widok trzeba by przebudowywać przy
każdym raporcie z każdego węzła, a czyta go **człowiek**, czyli rzadko. Tak, jak jest
teraz, nie kosztuje nic, dopóki nikt nie pyta.

Zysk jest jeszcze jeden, mniej oczywisty: **nie ma drugiego widoku, który mógłby się
rozjechać z pierwszym.** `Snapshot` zostaje jedynym publikowanym obrazem i ma w sobie
wyłącznie to, czego potrzebuje ścieżka szybka. Widok dla człowieka powstaje na żądanie,
w wątku będącym właścicielem modelu, i umiera zaraz po odesłaniu.

Wątek API **blokuje się** na `future.result()` i jest to w porządku: zatrzymuje to jedno
żądanie HTTP, a nie hub. To jest właśnie powód, dla którego adapter mieszka we własnym
wątku.

> **Biblioteka:** standardowa, `ThreadingHTTPServer` (**H7**, §13). Pierwotnie planowane
> było FastAPI + uvicorn w osobnym wątku — daje OpenAPI za darmo, co ma znaczenie, gdy opis
> API skonsumuje panel konfiguracyjny. Czy do tego wrócić — `zadania/CR-03.md`.

### 10.8 MQTT — potem

Jednostką prezentacji jest **kanał**, nie węzeł i nie endpoint — wynika to wprost
z [`01-model-danych.md`](../lagunbus-spec/01-model-danych.md) §5.3, gdzie kanał jest tym,
co człowiek nazywa i widzi jako jedną rzecz.

```
Węzeł 0x02A, przekaźnik 4× z pomiarem prądu

  ep4 Output ch1  ┐
  ep8 Sense  ch1  ┘ ──►  jedna encja HA: światło z pomiarem mocy
  ep12 Sense ch0  ────►  osobna encja: temperatura w rozdzielnicy
```

Kierunek jest jeden: **hub → Home Assistant**, tak jak w `zigbee2mqtt`. HA pokazuje
i steruje, ale nie konfiguruje ([`KONFIGURACJA.md`](KONFIGURACJA.md) §13.1).

Awaria brokera nie ma jak wpłynąć na magistralę — to ta sama reguła, która każe pętli
grzewczej mieszkać w węźle
([`00-wprowadzenie.md`](../lagunbus-spec/00-wprowadzenie.md) § Drugi wyjątek).

---

## 11. Jak to zmierzymy

Nie po to, żeby bronić progu, tylko żeby **widzieć trend**. Degradację ma być widać na
wykresie, zanim stanie się odczuwalna — to ta sama gwarancja, którą specyfikacja stawia
węzłom ([`00-wprowadzenie.md`](../lagunbus-spec/00-wprowadzenie.md) § Trzy gwarancje).

| Miara | Jak | Próg uwagi |
|---|---|---|
| **Dopasowanie powiązania** | `perf_counter_ns()` wokół ścieżki szybkiej | p99 > 5 ms |
| **Odbiór → nadanie** | znacznik jądra `SO_TIMESTAMPING` vs chwila `send()` | p99 > 10 ms |
| **Klawisz → przekaźnik** | pomiar na magistrali albo drugim węzłem | > 50 ms |
| **Głębokość kolejki wolnej** | licznik | rośnie trwale |
| **Porzucone ramki** | licznik per powód | cokolwiek ponad zero |

Histogram opóźnień jest **wbudowany na stałe**, nie włączany flagą, i wystawiany przez MQTT
razem z resztą diagnostyki. Kosztuje jedno odejmowanie na ramkę.

### 11.1 Traffic per bus

`GET /health` carries, for every bus, frames received, sent and dropped at sending, the
rate across the last 3 s and an estimated load ([`zadania/zrobione/FEAT-05.md`](../zadania/zrobione/FEAT-05.md);
the code in [`core/busload.py`](lagunhub/core/busload.py)).

- **The counters grow from the start and are never reset** — they, not the rate, are
  what the hub promises. Two readings and `uptime` give any window; hourly histograms
  and history belong to whoever collects them ([`zadania/FEAT-08.md`](../zadania/FEAT-08.md)),
  not to the hub.
- **Counted where the frame passes, one writer per counter**: received in the RX thread
  before the slow queue (which may overflow), sent by the fast path in the binding engine,
  sent by CORE in `_send`. Summed on reading, so no locks on the fast path. Sent frames are
  counted apart from received ones because SocketCAN does not hand a socket its own
  frames (§4.2).
- **The rate** is CORE's: it samples the counters once a second on its own deadline (§7.2)
  and divides the difference across the window.
- **The load is an estimate from above**: the bits of each frame with worst-case bit
  stuffing (160 for eight bytes), divided by the bitrate from `hub.yaml`, found by the
  channel. A bus the file does not name (`--udp`, `--bus` past the file) has no
  percentage. The view shows it with `≈`.
- The slow queue's `overflowed` stays a single number — the queue is one for all buses.
  Error frames and `bus-off` need SocketCAN on hardware (§14.1).

---

## 12. Układ projektu

Dokumentacja po polsku, **kod i nazwy po angielsku** — tak jak w samej specyfikacji, gdzie
identyfikatory są angielskie, a proza polska.

`ProjectLagun/` jest **katalogiem roboczym, nie repozytorium**. Repozytoria są dwa i leżą
obok siebie: [`lagunbus-spec/`](../lagunbus-spec/) — specyfikacja, źródło prawdy — oraz
`hub/`, czyli ten projekt.

```
hub/                         ← repozytorium
├── ARCHITEKTURA.md          ten dokument
├── KONFIGURACJA.md          powiązania, źródła, adaptery
├── README.md                uruchomienie — na komputerze i na Raspberry Pi
├── config/                  przykładowa konfiguracja instalacji
├── pyproject.toml
├── lagunhub/
│   ├── protocol/            SPECYFIKACJA W KODZIE — nic o hubie
│   │   ├── frame.py         05 §1 — identyfikator 29-bitowy
│   │   ├── bus/             05 — transport: SocketCAN, wirtualny, odtwarzanie
│   │   ├── values.py        02 — typy danych
│   │   ├── quantities.py    02 §3 — wielkości i skale
│   │   ├── registry.py      registry/ — GENEROWANY
│   │   └── endpoints/       types/ — katalog typów endpointów
│   │
│   ├── core/                RDZEŃ — nic o tym, kto z hubem rozmawia
│   │   ├── model.py         Installation, NodeModel, Snapshot, BindingSpec
│   │   ├── rx.py            RxThread — odbiór i rozdział ruchu
│   │   ├── bindings.py      BindingEngine — ścieżka szybka
│   │   ├── hub.py           CoreThread — pętla, kolejka, terminy
│   │   ├── schedule.py      kopiec terminów; CORE nie ma wątku zegara
│   │   ├── transactions.py  TidAllocator, TransactionManager
│   │   ├── segments.py      kodek segmentów i reasembler
│   │   ├── commissioning.py ZGŁOSZENIE/PRZYDZIAŁ, tryb przyjmowania
│   │   ├── strangers.py     node at an unknown address → PENDING (§8.3)
│   │   ├── enumeration.py   odczyt tożsamości i deskryptora
│   │   ├── reconcile.py     uzgadnianie konfiguracji z węzłami
│   │   ├── readout.py       reading every attribute on request (§8.5)
│   │   ├── events.py        strumień zdarzeń dla adapterów
│   │   ├── metrics.py       histogram opóźnień
│   │   ├── busload.py       traffic per bus: counters, rate, load (§11.1)
│   │   ├── config/          decyzje człowieka: wczytanie, walidacja, ZAPIS
│   │   └── store/           model w SQLite, zapis z opóźnieniem
│   │
│   ├── drivers/             sprzęt pod magistralą: ustawienie, nie ramki (FEAT-01)
│   │   ├── base.py          AdapterDriver: plan / apply — najpierw różnica, potem zmiana
│   │   ├── socketcan/       ip link — każdy interfejs CAN jądra
│   │   └── usr_can315/      komendy AT (Ethernet↔CAN)
│   │
│   ├── api/                 nazwana lista operacji
│   │   ├── errors.py        Problem, ApiError — kontrakt, bez core/
│   │   ├── views.py         widoki — kontrakt, bez core/
│   │   └── hub.py           HubApi: operacje i droga do CORE
│   ├── adapters/
│   │   ├── http/            serwer huba: REST + SSE, token
│   │   ├── client/          klient chodzącego huba: HTTP + SSE, protokół Hub
│   │   ├── tui/             widok pełnoekranowy nad tym klientem
│   │   └── mqtt/            Home Assistant — §14.2
│   ├── buses.py             start: sterownik ustawia magistralę, potem gniazdo (FEAT-01)
│   ├── buswatch.py          a SocketCAN bus down, not by the hub, is a failure — reported, brought up (BUG-04)
│   ├── monitor.py           podgląd ruchu i modelu
│   └── cli.py               wiersz poleceń
│
└── tests/
    └── node/                atrapa węzła — zachowanie typów, definicje z lagunhub.protocol
```

Podział na trzy, a nie dwa, i to jest tu najważniejsze:

> **`protocol/` to specyfikacja w kodzie, `core/` to hub.**

Granica między nimi **nie jest zgadywana** — ma już drugiego konsumenta. Symulowane
węzły importują `protocol/` i nie mają prawa dotknąć `core/`; gdyby kiedyś musiały,
byłby to dowód, że coś jest po złej stronie. Dzień, w którym warto wydzielić
`lagunbus-core` (**H5**), jest dniem, w którym `protocol/` wyprowadza się bez niczego
innego.

Granica między `core/` a `adapters/` jest natomiast **wciąż zgadywana** — dostała
pierwszego konsumenta w serwerze huba, drugiego w kliencie w terminalu. Dlatego `api/` leży obok,
a nie w żadnym z nich.

Od cleanupu przed wersją 0.5 `api/` jest pakietem, podzielonym wedle tego, co od czego
zależy: `errors.py` i `views.py` to **kontrakt** i nie importują niczego z `core/`,
`hub.py` to implementacja. Klient chodzącego huba (widok) potrzebuje wyłącznie
kontraktu i dzięki temu nie ładuje rdzenia — pilnuje tego `tests/test_boundaries.py`.
`lagunhub/api/__init__.py` ładuje `hub.py` leniwie, bo inaczej samo `import
lagunhub.api.views` ciągnęłoby rdzeń przez paczkę nadrzędną.

Jedno `pyproject.toml` z **jednym pakietem**: `lagunhub`. Atrapa węzła dzieli z hubem
protokół (§12.2), więc mieszka w tym repozytorium — ale w `tests/`, bo do instalacji
wchodzić nie ma.

### 12.1 Rejestr jest generowany, nie przepisywany

`lagunhub/protocol/registry.py` powstaje skryptem z [`lagunbus-spec/registry/*.yaml`](../lagunbus-spec/registry/).
Ręczne przepisanie trzynastu kodów wyniku i szesnastu rodzajów ramek **rozjedzie się ze
specyfikacją przy pierwszej zmianie** i nikt tego nie zauważy, dopóki coś nie przestanie
działać w sposób trudny do wyjaśnienia. Generator jest tańszy niż jedno takie dochodzenie.

Nazwy rodzajów zostają **po angielsku**, choć rejestr trzyma je po polsku — mapowanie robi
generator: `ZDARZENIE → MessageKind.EVENT`, `ODPOWIEDŹ_DŁUGA → MessageKind.LONG_RESPONSE`.
Tabela tłumaczeń siedzi w jednym miejscu, w generatorze, i jest jedyną rzeczą do
zaktualizowania, gdy w specyfikacji przybędzie rodzaj.

### 12.2 Kodek jest wspólny z drugą stroną kontraktu

Cokolwiek rozmawia z hubem, musi kodować i dekodować **dokładnie te same ramki** co on.
Dwie niezależne implementacje kodeka dałyby testy, które przechodzą, bo obie strony mylą
się tak samo.

Dlatego atrapa węzła **importuje `lagunhub.protocol.frame`, `lagunhub.protocol.values`
i `lagunhub.protocol.endpoints`** — i dlatego mieszka w tym repozytorium, w
[`tests/node/`](tests/node/). Zmiana w kodeku jest wtedy jednym commitem, a nie wydaniem
pakietu i aktualizacją zależności po drugiej stronie.

Przed wersją 0.5 ta sama zależność szła do `simulators/lagunsim` — pakietu, który instalował
się **razem z hubem**. To była pomyłka w paczkowaniu, nie w zależności: na Raspberry Pi
ma lądować hub i nic więcej. Kod został tam, gdzie jest używany, czyli w testach.

Warunek, przy którym to zmieniamy, warto zapisać już teraz: **gdy druga strona zacznie
potrzebować czegoś, czego hub nie potrzebuje**, albo gdy zależność zacznie iść w drugą
stronę. Wtedy `frame.py`, `values.py`, `endpoints/` i `registry.py` przenoszą się do
`lagunbus-core` — i dopiero wtedy rozdzielenie repozytoriów zaczyna mieć sens.

The simulator run by hand was expected to be the first test of this, and it did not need
`lagunbus-core`: it does not take the hub's codec at all, but the node library's
(`../lagunbus-node`, in C, the same as the boards — zadania/zrobione/FEAT-02.md N10). The only
thing the two sides share is the UDP datagram format, written down once and pinned by
the same byte vector on both sides. H5 stays open, for the same condition.

Widać już jednego kandydata: hub **przepuszcza wszystko** i nie ma filtrów, a węzeł ma bardzo
wąskie filtry sprzętowe ([`05`](../lagunbus-spec/05-mapowanie-can.md) §3). Atrapa ich nie
odwzorowuje, więc będzie to pierwsza rzecz, której w hubie nie ma.

### 12.3 Katalog typów przepisujemy ręcznie i pilnujemy tego sami

`registry/` jest maszynowo czytelny i generator go pilnuje. Katalog
[`types/`](../lagunbus-spec/types/) jest prozą w markdownie — więc `lagunhub/protocol/endpoints/` jest
**jedynym w projekcie miejscem przepisanym z ręki**, bez niczego, co porównywałoby je
ze specyfikacją.

> **Jest to decyzja, nie przeoczenie.** Wyciąganie definicji z prozy dałoby parser
> markdownu, który psuje się przy każdej zmianie redakcyjnej — a katalog typów zmienia
> się rzadko i w sposób, który i tak trzeba przeczytać ze zrozumieniem.

Cena jest realna i warto ją nazwać: **literówka w numerze elementu przejdzie niezauważona**
wszędzie tam, gdzie nie sięgają wektory z [`PRZYKLADY.md`](../lagunbus-spec/PRZYKLADY.md).
Dziś nie sięgają do progów `Sense`, liczników `Diagnostics` i atrybutów `Scenes`.

Dwie rzeczy, które to częściowo nadrabiają i są tanie:

- **każdy nowy typ dostaje wektor testowy**, choćby jeden — najlepiej dopisany do
  `PRZYKLADY.md`, bo wtedy sprawdza obie strony naraz,
- **atrapa i hub czytają te same definicje** (§12.2), więc literówka daje ten sam
  błąd po obu stronach i nie ukryje się jako „niezgodność implementacji".

---

## 13. Punkty otwarte

| Poz. | Czego dotyczy | Kiedy rozstrzygniemy |
|---|---|---|
| **H1** | czy złagodzić §4.2 w specyfikacji, czy trzymać 1 ms | **settled**: the specification adopted 50 ms — §5.4 |
| **H2** | kształt tematów MQTT i mapowanie kanał → encja HA | razem z adapterem MQTT (§14) |
| **H6** | uwierzytelnianie API | **rozstrzygnięte**: jeden token na hub, wymagany zawsze, losowany przy pierwszym starcie i zapisywany z prawami `0600`. Nasłuch domyślnie na pętli lokalnej — [`adapters/http/auth.py`](lagunhub/adapters/http/auth.py) |
| **H9** | podział pakietów | **zrobione**: `protocol/` i `core/` (§12); granica `adapters/` czeka na drugiego konsumenta |
| **H11** | `endpoints/` nie ma odpowiednika `--check`, który pilnuje rejestru | **rozstrzygnięte: pilnujemy ręcznie** — §12.3 |
| **H7** | FastAPI czy coś lżejszego na RPi | **rozstrzygnięte: biblioteka standardowa**, `ThreadingHTTPServer`. Uzasadnienie i cena w [`adapters/http/server.py`](lagunhub/adapters/http/server.py). Argument o puli wątków jest nieścisły (FastAPI sam odsyła zwykłe `def` do puli) — do ponownego rozważenia w `zadania/CR-03.md` |
| **H8** | czy specyfikacja ma kiedyś dostać maszynowo czytelny opis typów, tak jak ma `registry/` | odłożone; dziś przepisujemy ręcznie (§12.3) |
| **H3** | format pliku konfiguracji powiązań | **rozstrzygnięte** — [`config/`](config/) |
| **H4** | czy `VirtualBus` ma udawać także błędy magistrali i `bus-off` | gdy zacznie brakować testów diagnostyki |
| **H5** | czy wydzielić `lagunbus-core` i rozdzielić repozytoria | §12.2 — gdy druga strona kontraktu zacznie potrzebować czegoś, czego hub nie potrzebuje |

---

## 14. Czego jeszcze nie ma

### 14.1 Sprawdzenie na sprzęcie

Nie jest to funkcja, tylko dług — i rośnie z każdą zmianą, bo rozwój dzieje się na macOS.
Jak go spłacić, opisuje [`README.md`](README.md) § Raspberry Pi.

| Co | Dlaczego niesprawdzone |
|---|---|
| **SocketCAN** | `AF_CAN`, `bind()`, nieblokujący `recv` i `ENOBUFS` znamy z dokumentacji; na macOS ten kod **nigdy się nie wykonał** — jest za `getattr(socket, "AF_CAN", -1)` |
| **własne ramki** | gniazdo ich nie odbiera, więc podgląd połknie komendę huba (§4.2); wiemy, nie widzieliśmy |
| **opóźnienie na RPi** | 22 µs zmierzone na magistrali w procesie, bez ani jednego wywołania systemowego |
| **`bus-off` i błędy magistrali** | `VirtualBus` sortuje kolejkę, zamiast przegrywać arbitraż bit po bicie (**H4**) |
| **`fsync` na karcie SD** | dziesiątki do setek ms; dlatego zapis jest w wątku STORE (§5.2). Writing `joined` to the file happens on CORE, once per joined node — the same as every write from the API; a wave of forty joins is forty small writes, not measured on the card |
| **a restart with nodes from the file** (§8.6) | tested in-process and once on `--udp` with `lagun-sim`; on the RPi: `lagun-hub run`, nodes admitted, `Ctrl-C`, `lagun-hub run`, `G` at once in the view |
| **zegar** | RPi bez RTC wstaje w 1970 i wraca do teraz po `ntp` |
| **wiele magistral** | zaprojektowane i skompilowane, ale każdy test używa jednej |
| **a SocketCAN bus going down** (§9.3) | tested on a fake `/sys` and a fake socket only (`zadania/zrobione/BUG-04.md`). On the RPi: `ip link set can0 down` under traffic → `FAILURE`, back within ~1 s; the same without the sudo rule → `GAVE UP` at once, then `up` by hand → working again; "apply" in `Buses` → no alarm; start with `can0` down; a USB adapter out and back in → socket reopened. Also to confirm: `send` while down gives `ENETDOWN` every time, and why the hub saw `ENETDOWN` right at start on 2026-10-06 (a bare `bind` to a down interface should not give it) |
| **magistrala przez USR-CAN315** | sterownik konfiguracyjny jest (`drivers/usr_can315/`), transportu ramek `PROTOL` po TCP nie ma — `zadania/FEAT-10.md` |

### 14.2 Adapter MQTT i Home Assistant

- `adapters/mqtt/` nad tym samym API — **nie zastępuje serwera ani widoku**, dokłada się,
- jednostką prezentacji jest **kanał**, nie węzeł i nie endpoint
  ([`01-model-danych.md`](../lagunbus-spec/01-model-danych.md) §5.3); przekaźnik 4×
  z pomiarem prądu to 4 encje kanałowe i jedna temperatura, nie 9 bytów,
- sterowanie `lagun/bridge/request/*` → `lagun/bridge/response/*` z opcjonalnym polem
  korelacji — kształt sprawdzony w `zigbee2mqtt`; kierunek jeden: hub → HA
  ([`KONFIGURACJA.md`](KONFIGURACJA.md) §13.1),
- `Purpose = INDICATOR` nie trafia do „zgaś wszystkie światła",
- **zabicie brokera nie zmienia ani jednej liczby w histogramie opóźnień**,
- API nie rośnie o operację istniejącą wyłącznie dla MQTT.

### 14.3 Zapytanie

- `QUERY` na ścieżce szybkiej (§4.4), odpowiedź `UNAVAILABLE` **natychmiast**, sięganie
  po wartość dopiero potem, w tle — kolejne zapytanie dostaje już wartość,
- `QueryEnabled` zapisywany przy **zmianie mapowania wejścia**, nie przy zmianie
  dostępności źródła: flaga jest trwała, a jej przepisywanie zużywa pamięć węzła,
- źródła spoza instalacji: `mqtt(...)`, `const(...)` z [`KONFIGURACJA.md`](KONFIGURACJA.md) §6.

### 14.4 Utrzymanie

- usługa `systemd`: użytkownik, uprawnienia, restart po zaniku zasilania, dziennik,
  rotacja. **Dziś jej nie ma świadomie** — hub zmienia się kilka razy dziennie i łatwiej
  go zatrzymać `Ctrl-C` niż `systemctl`,
- obecność: próg `2 × min(ReportInterval)`, odczyt `Uptime`, trzy próby co 10 s (§7.3);
  `Uptime` niższy niż poprzednio to restart, którego hub nie zauważył,
- dziennik zdarzeń i rozjazdów — **do decyzji operatora, nie do zgadywania**
  ([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.3),
- kopia katalogu konfiguracji,
- zablokowanie nie cofa członkostwa w grupach zapisanego wcześniej w węźle (§8.4) — komenda
  grupowa wciąż do niego dociera.

### 14.5 Widok

- **listy odświeżane zdarzeniem, nie co 2 s.** Dziś widok przeładowuje je od razu tylko po
  własnych akcjach; zmiana z instalacji (węzeł wchodzi, klik, raport) dociera najwyżej
  dwie sekundy później, bo dolny panel czyta `frames()`, a nie `stream()`. Słuchanie
  `GET /events` i przeładowanie na zdarzenie zrobiłoby z odpytywania siatkę
  bezpieczeństwa (`REFRESH` w [`adapters/tui/state.py`](lagunhub/adapters/tui/state.py)).

---

## 15. Czego świadomie nie robimy

| Co | Dlaczego |
|---|---|
| Aktualizacja firmware przez magistralę | segmenty jej nie uniosą; wymaga własnego mechanizmu ([`05-mapowanie-can.md`](../lagunbus-spec/05-mapowanie-can.md) §6.9) |
| Wymiana modułu z zachowaniem konfiguracji | the specification defers it past v1 ([`DECYZJE.md`](../lagunbus-spec/DECYZJE.md), "Wymiana urządzenia poza zakresem v1") |
| Harmonogramy, tryby domu, obecność ludzi | to jest Home Assistant, nie hub (§1) |
| Grupy i sceny zapisywane do węzłów | konfiguracja je opisuje, uzgadnianie przepisuje na razie tylko atrybuty; `GetMembership` jest segmentowane, a scen hub nie umie odczytać |
| Zabezpieczenie kryptograficzne magistrali | rozstrzygnięte przeciw w specyfikacji |
| Optymalizacja ścieżki szybkiej poniżej milisekundy | budżet jest luźny; wracamy, gdy pomiar da powód (§5.1) |
| Polecenia jednorazowe w CLI | do skryptów jest REST; w terminalu jest tylko widok, dopóki ktoś nie pokaże, że mu ich brakuje (§10.3) |
