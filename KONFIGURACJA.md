# Konfiguracja huba — szkic

> **Szkic, v0.1.** Nakreślenie kształtu, nie rozstrzygnięcie. Formaty i nazwy pól się zmienią;
> podział na trzy rodzaje stanu (§1) — nie powinien.
>
> Architektura: [`ARCHITEKTURA.md`](ARCHITEKTURA.md).

---

## 1. Trzy rodzaje stanu, nie jeden

Hub trzyma trzy rzeczy, które łatwo pomylić, bo wszystkie wyglądają jak „ustawienia".
Różni je **pochodzenie**, a z pochodzenia wynika wszystko dalsze: gdzie mieszkają, co jest
w kopii zapasowej i co się dzieje, gdy się zgubią.

| | Skąd pochodzi | Da się odtworzyć bez człowieka | Gdzie mieszka |
|---|---|---|---|
| **Model instalacji** | odczytany z węzłów | **tak** — wystarczy poczekać | baza |
| **Decyzje człowieka** | wpisane przez operatora | **nie, nigdy** | plik |
| **Ustawienia huba** | wpisane przy wdrożeniu | nie, ale są krótkie | plik |

Rozróżnienie pierwszego od drugiego jest wprost z
[`07-hub.md`](../lagunbus-spec/07-hub.md) §5.3 i jest jednym z ostrzejszych zdań w całej
specyfikacji:

> **Z węzłów wraca liczba, nigdy znaczenie.**

Hub odczyta z instalacji, że endpoint 4 węzła `0x02A` należy do grupy `5`. Nie odczyta,
że grupa `5` znaczy „światła na parterze" — bo tego w węźle nie ma i nie będzie.

```
        CO ODKRYTE                          CO ZDECYDOWANE
   ┌──────────────────────┐            ┌──────────────────────┐
   │ Uid, DeviceType      │            │ nazwy                │
   │ deskryptor endpointów│            │ pomieszczenia        │
   │ wersje firmware      │            │ POWIĄZANIA           │
   │ Uid → adres          │            │ znaczenie grup       │
   │ stany atrybutów      │            │ zawartość scen       │
   │ numery grup (liczby) │            │ źródła wejść         │
   └──────────┬───────────┘            └──────────┬───────────┘
              │                                   │
        baza (SQLite)                       plik w gicie
        odtwarzalna                         NIEODTWARZALNA
        kopia = wygoda                      kopia = warunek istnienia
```

Praktyczny wniosek, który warto powiedzieć wprost: **kopia plików konfiguracyjnych jest
kopią instalacji. Kopia bazy jest tylko oszczędnością czasu.** Baza odbuduje się sama
w ciągu jednego `ReportInterval` ([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.1); plik nie
odbuduje się nigdy.

### 1.1 Granica biegnie także wewnątrz bazy

Podział jest ostry również w tym, **czego baza nie zapisuje**. Nazwa węzła jest decyzją
człowieka, więc mieszka wyłącznie w pliku — mimo że model trzyma ją w pamięci i kusiłoby
zapisać ją przy okazji.

Zapisana w obu miejscach byłaby **drugim źródłem prawdy**, a skutek widać dopiero w tej
jednej sytuacji, w której to ma znaczenie: hub, który zgubił pliki, pokazywałby instalację
**z nazwami i bez powiązań**. Wyglądałaby na skonfigurowaną, a nie byłaby.

> **Utrata konfiguracji ma być widoczna.** Model bez nazw jest niewygodny i natychmiast
> mówi, czego brakuje; model z nazwami i bez reszty decyzji kłamie.

---

## 2. Trzeci podział: gdzie konfiguracja skutkuje

Ten sam plik niesie rzeczy, które trafiają w trzy różne miejsca — i to rozstrzyga, co hub
z nimi robi.

| Kategoria | Przykład | Co robi hub |
|---|---|---|
| **Żyje w hubie** | powiązania, nazwy, pomieszczenia | trzyma u siebie, nikomu nie wysyła |
| **Przepisywana do węzłów** | grupy, `QueryEnabled`, `ReportInterval`, atrybuty konfiguracyjne typów, sceny | **uzgadnia** — §7, §8 |
| **Konfiguruje sam hub** | magistrale, broker MQTT, dziennik | czyta raz przy starcie |

Druga kategoria jest jedyną, w której konfiguracja **opisuje stan czegoś, co jest poza
hubem** — i dlatego jedyną, która potrzebuje uzgadniania zamiast zwykłego odczytu.

---

## 3. Szkic plików

```
config/
├── hub.yaml          magistrale, adaptery, ustawienia huba
├── nodes.yaml        alias → Uid, nazwy, pomieszczenia
├── bindings.yaml     powiązania i źródła wejść
└── secrets.env       hasła — POZA gitem
```

Podział na cztery pliki, a nie jeden, z jednego powodu: **zmieniają się w zupełnie różnym
tempie**. `hub.yaml` piszemy raz przy wdrożeniu, `bindings.yaml` edytujemy przy każdej
zmianie w domu. Trzymanie ich razem znaczyłoby, że kopia sprzed tygodnia miesza jedno
z drugim.

---

## 4. Adresowanie: alias, nie `Uid`

Napięcie, które trzeba rozstrzygnąć na samym początku:

| | Chce |
|---|---|
| maszyna | `Uid` — tożsamość, przeżywa przeadresowanie i wymianę bezpiecznika |
| człowiek | `kuchnia-klawisz` — coś, co da się przeczytać w pliku |

Rozstrzygnięcie: **kluczem jest `Uid`, a alias jest polem.** Reszta plików odwołuje się
aliasem — albo `Uid`-em, gdy aliasu nie ma.

```yaml
# nodes.yaml
nodes:
  "0102030405060708":
    alias: kitchen-switch
    name: "Klawisz lewy"
    room: kuchnia

  "1112131415161718":
    alias: kitchen-relay
    name: "Lampa nad stołem"
    room: kuchnia
```

Kusi, żeby kluczem zrobić alias — czyta się lepiej. Ale wtedy plik wygląda, jakby hub
**wiedział, że pod tym wpisem jest `kitchen-relay`**; wie tylko tyle, że ktoś tak nazwał
**ten konkretny `Uid`**. Struktura ma mówić to, co jest prawdą.

Dwie rzeczy wychodzą z tego za darmo:

- **YAML sam pilnuje niepowtarzalności `Uid`** — a to jest ta własność, na której stoi
  cała reszta. Niepowtarzalność aliasu jest kosmetyczna i wystarczy jej walidator.
- **API dokładające węzeł nie musi zmyślać aliasu.** Zna `Uid` i nic poza tym; zapisuje
  wpis, a człowiek dopisze `alias:`, gdy zechce. Punkt **K9** znika, zamiast być obchodzony.

Alias zostaje **wyłącznie wygodą zapisu**: hub tłumaczy go przy wczytaniu i dalej nie
istnieje. Zasada z [`07-hub.md`](../lagunbus-spec/07-hub.md) §2 („wszystko kluczowane
`Uid`") obowiązuje więc już w samym pliku, a nie dopiero po jego przetworzeniu.

### 4.1 Ta sama reguła dotyczy grup

```yaml
groups:
  1: { name: "Wszystkie światła" }
  5: { name: "Salon" }
```

**Kluczem jest numer, bo to on jedzie w polu `ADRES` ramki.** Nazwa jest etykietą,
dokładnie jak alias przy węźle — i tak samo wolno się nią posługiwać w reszcie plików.

Reguła, która z tego wychodzi i warto ją mieć zapisaną, bo zastosuje się do wszystkiego,
co jeszcze dojdzie:

> **Kluczem jest to, co jedzie w ramce. Nazwa jest etykietą.**

Nazwy wiszą przy **kanale**, nie przy endpoincie — bo kanał jest jednostką, którą człowiek
widzi jako jedną rzecz ([`01-model-danych.md`](../lagunbus-spec/01-model-danych.md) §5.3).
Wyjście i jego pomiar prądu nazywają się raz, nie dwa razy.

---

## 5. Powiązania

```yaml
# bindings.yaml
bindings:
  # klawisz → przekaźnik
  - from: kitchen-switch.ep4.Click
    to:   kitchen-relay.ep4.Toggle

  # jeden do wielu — kolejność konfiguracji jest kolejnością wykonania
  - from: hall-switch.ep4.Click
    to:
      - hall-relay.ep4.Toggle
      - stairs-relay.ep4.Toggle

  # gdy mają ruszyć RAZEM — komenda grupowa, nie lista
  - from: hall-switch.ep5.Click
    to:   group(all-lights).Off
```

Notacja `alias.epN.Element` jest celowo dosłowna: mówi wprost, który endpoint i który element,
bo tak właśnie wygląda ramka. Skrót przez kanał (`kitchen-relay.ch1.Toggle`) jest kuszący,
ale **kanał może obejmować kilka endpointów** i wtedy przestaje być jednoznaczny — do
rozstrzygnięcia jako **K1**.

Trzy rzeczy, które plik musi umieć wyrazić, bo wynikają ze specyfikacji:

| Kształt celu | Kiedy | [`07-hub.md`](../lagunbus-spec/07-hub.md) |
|---|---|---|
| komenda adresowana | gdy zależy na potwierdzeniu | §3.2 |
| komenda grupowa | gdy cele mają ruszyć jednocześnie | §3.2, §6 |
| **zapis atrybutu** | gdy celem jest wartość, nie akcja | §3.2 — patrz §6 poniżej |

I jedna, której **nie wolno** wyrazić: łańcucha. Powiązanie, którego źródłem jest skutek
innego powiązania, jest zabronione ([`07-hub.md`](../lagunbus-spec/07-hub.md) §4.1) — a w tej
architekturze jest wręcz niemożliwe do zbudowania
([`ARCHITEKTURA.md`](ARCHITEKTURA.md) §3.2). Plik nie potrzebuje na to składni; potrzebuje
jedynie, żeby walidator nie udawał, że coś takiego zadziała.

---

## 6. Źródła danych

To jest druga rzecz, o którą pytałeś — i warto zacząć od tego, że **w specyfikacji nie jest
osobnym mechanizmem**. „Wejście ← źródło" to zwykłe powiązanie, którego celem jest **zapis
atrybutu**, a nie komenda ([`07-hub.md`](../lagunbus-spec/07-hub.md) §3.2,
[`04-interakcje.md`](../lagunbus-spec/04-interakcje.md) §8).

Osobna sekcja w pliku istnieje mimo to, bo źródło bywa **spoza instalacji** — a tego zwykłe
powiązanie nie opisuje.

```yaml
# bindings.yaml
sources:
  # źródło w instalacji → kompiluje się do zwykłego powiązania
  - into: boiler.ep4.OutdoorTemperature
    from: garden-sensor.ep4.Value

  # źródło poza instalacją → hub trzyma wartość u siebie i nią odpowiada
  - into: boiler.ep4.SetpointOverride
    from: mqtt("home/heating/setpoint")

  # wartość stała — do rozruchu i do testów
  - into: boiler.ep4.OutdoorTemperature
    from: const(5.0)
```

### 6.1 Konfiguracja źródeł generuje `QueryEnabled`

Ta sekcja robi coś, czego żadna inna nie robi: **zmienia stan węzła**.

[`04-interakcje.md`](../lagunbus-spec/04-interakcje.md) §8.6 mówi, że węzeł nie pyta o wejście,
którego bit w `QueryEnabled` jest zgaszony, a domyślnie zgaszone są wszystkie. Więc:

```
wpis w sources:  boiler.ep4.OutdoorTemperature ← garden-sensor
                              │
                              ▼
        hub zapala bit 0 w QueryEnabled endpointu 4 węzła boiler
                              │
                              ▼
        ZAPIS 0xF6 = 0b01   ← atrybut TRWAŁY, więc zapisujemy przy ZMIANIE
                              MAPOWANIA, nigdy przy zmianie dostępności
```

Ostatnia linijka jest ostrzeżeniem ze specyfikacji, a nie ozdobnikiem: `QueryEnabled` jest
trwały, a jego przepisywanie zużywa pamięć nieulotną węzła. Od sygnalizowania „źródło właśnie
zamilkło" jest `UNAVAILABLE`, nie flaga.

Wniosek dla implementacji: **`QueryEnabled` wylicza się z pliku konfiguracji, nie ze stanu
świata.** Źródło może milczeć godzinę i bit ma zostać zapalony.

---

## 7. Konfiguracja endpointów

Każdy typ endpointu niesie własne atrybuty konfiguracyjne — `RW`, trwałe. Klawisz ma
`DebounceTime` i `HoldThreshold`, wyjście `PowerOnBehaviour` i `AutoOffTime`, a **wszystkie
endpointy funkcjonalne** mają wspólne `Enabled`, `ReportInterval`, `ReportMinGap`
i `ReportHysteresis` ([`01-model-danych.md`](../lagunbus-spec/01-model-danych.md) §4).

Wszystkie mieszkają **fizycznie w węźle**, więc należą do drugiej kategorii z §2 i podlegają
uzgadnianiu (§8) — plik mówi, jak ma być, hub dopilnowuje, żeby tak było.

```yaml
# nodes.yaml
nodes:
  kitchen-switch:
    uid: "0102030405060708"
    room: kuchnia
    endpoints:
      4:
        Invert: true            # fakt MONTAŻOWY — jak podpięto przycisk
        DebounceTime: 25
        HoldThreshold: 600
        groups: [all-lights]    # przez komendy Groups, nie przez zapis atrybutu
      5:
        DebounceTime: 25

  kitchen-relay:
    uid: "1112131415161718"
    endpoints:
      4:
        Purpose: LIGHT          # rozstrzyga, czy należy do „zgaś wszystkie światła"
        PowerOnBehaviour: OFF
        AutoOffTime: 0
        ReportInterval: 300
```

Cztery rzeczy warte powiedzenia od razu:

**Nazwa atrybutu, nie numer.** W pliku piszemy `DebounceTime`, nie `0x10`. Tłumaczenie robi
katalog [`types/`](../lagunbus-spec/types/) — ten sam, który daje walidację offline (§10).
Numer byłby nieczytelny i nie dałby się sprawdzić bez sprzętu.

**Czego tu nie ma: stanu i nastaw.** `Output.OnOff` jest stanem, nie konfiguracją — hub nie
ustawia go plikiem, tylko komendą. Granica biegnie dokładnie po kolumnach katalogu typów:

| Dostęp | Trwałość | Czym jest | Skąd się bierze |
|---|---|---|---|
| `RW` | `P` | **konfiguracja** | plik, uzgadniana (§8) |
| `RW` | `V` | nastawa robocza | komenda albo zapis w czasie pracy |
| `RO` | — | stan | odczyt z węzła, nigdy z pliku |

**Atrybut, którego w pliku nie ma, nie jest dotykany.** Nie ma czegoś takiego jak „przywróć
domyślną" — od tego jest `FactoryReset`. Plik opisuje odstępstwa od tego, co węzeł ma
fabrycznie, a nie pełny stan jego pamięci.

**Grupy i sceny nie są atrybutami.** Przynależność do grup prowadzi osobny typ na `ep2`
i zmienia się **komendami**, nie zapisem ([`types/0x0004-groups.md`](../lagunbus-spec/types/0x0004-groups.md)),
a zawartość sceny węzeł potrafi zapisać, ale **nie potrafi odczytać**
([`07-hub.md`](../lagunbus-spec/07-hub.md) §2). Jedno i drugie wygląda w pliku jak zwykła
konfiguracja i jedno i drugie wymaga od huba innej drogi zapisu — to jest sprawa
implementacji uzgadniania, nie składni pliku.

### 7.1 Trzy różne czasy życia w jednym zapisie

Wiersze powyżej wyglądają jednakowo, a nie są tym samym — i będzie to miało znaczenie, gdy
powstanie panel konfiguracyjny:

| Przykład | Czym jest | Kto to ustawia |
|---|---|---|
| `Invert` | fakt montażowy | instalator, raz, przy podłączeniu |
| `DebounceTime`, `HoldThreshold` | strojenie pod konkretny przycisk | człowiek, gdy coś działa niewygodnie |
| `ReportInterval` | polityka całej instalacji | operator, zwykle globalnie |
| `Purpose` | znaczenie dla systemu nadrzędnego | operator, przy pierwszej konfiguracji |

Warto stąd jeden wniosek na przyszłość: **`ReportInterval` prosi się o wartość domyślną
w `hub.yaml`**, nadpisywaną per endpoint — bo ustawia się ją dla wszystkiego naraz, a nie
dla pojedynczego klawisza. Nie robimy tego teraz; odnotowane jako **K6**.

---

## 8. Uzgadnianie z węzłami

Część konfiguracji mieszka fizycznie w węzłach: przynależność do grup, `QueryEnabled`,
zawartość scen, `ReportInterval`. Hub ma tu jeden kierunek i jest on ostry:

> **Hub przepisuje do węzła to, co ma u siebie — i w tę stronę, nigdy w drugą.**
> ([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.3)

```
  plik konfiguracji            węzeł
  (stan pożądany)              (stan faktyczny)
        │                            │
        └────────► porównaj ◄────────┘
                      │
         zgodne ──────┴────── rozjazd
            │                    │
         nic nie rób       ZAPIS do węzła
                                 │
                          i ODNOTUJ w dzienniku
```

Odczyt z węzła służy więc **sprawdzeniu zgodności**, nigdy odtwarzaniu konfiguracji. Hub,
który zgubił plik, odtworzy z instalacji strukturę i **ani jednej decyzji operatora** —
grupy bez nazw, wejścia bez źródeł, sceny o nieznanej zawartości.

Dlatego nota implementacyjna ze specyfikacji jest tu wymaganiem: hub, który wykryje brak
własnej konfiguracji, **pokazuje człowiekowi listę rzeczy do skonfigurowania od nowa**,
a nie zgaduje z tego, co znalazł w węzłach. Instalacja działająca pozornie jest gorsza niż
instalacja jawnie niedokonfigurowana.

---

## 9. Ustawienia huba

```yaml
# hub.yaml
groups:
  1: { name: "Wszystkie światła" }
  5: { name: "Salon" }

buses:
  - name: can0        # parter
    interface: socketcan
    channel: can0
  - name: can1        # piętro
    interface: socketcan
    channel: can1

adapters:
  mqtt:
    broker: 192.168.1.10
    port: 1883
    username: lagun
    password: ${MQTT_PASSWORD}      # z secrets.env, NIE z gitu
    discovery_prefix: homeassistant

commissioning:
  accept_new: false                 # tryb przyjmowania; wygasa sam po 15 min
```

Nazwa magistrali (`can0`, ale też `parter`) jest **wyłącznie dla człowieka i dziennika** —
w ramce numeru magistrali nie ma i nigdy nie będzie
([`03-adresacja.md`](../lagunbus-spec/03-adresacja.md) §2.1). To hub wie, na którym
interfejsie widzi który węzeł, i jest jedynym miejscem w systemie, gdzie ta wiedza istnieje.

Sekrety idą przez podstawienie ze zmiennych środowiskowych. Powód jest prozaiczny i dlatego
łatwy do przeoczenia: **pliki konfiguracyjne mają leżeć w gicie**, bo są jedyną
nieodtwarzalną częścią instalacji (§1) — a hasło do brokera w gicie to hasło do brokera
w gicie.

---

## 10. Walidacja jest dwufazowa

Konfiguracja odwołuje się do endpointów i elementów, których hub przy starcie jeszcze nie
zna — bo po restarcie **nasłuchuje, a nie odpytuje**
([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.1). Stąd dwie fazy, a nie jedna:

| Faza | Kiedy | Co sprawdza | Skutek błędu |
|---|---|---|---|
| **statyczna** | przy wczytaniu pliku | składnia, nieznane aliasy, nieistniejące elementy **w katalogu typów** | hub **nie startuje** |
| **wobec instalacji** | gdy węzeł zostanie rozpoznany | czy ten węzeł naprawdę ma ten endpoint i tę cechę | wpis do listy usterek, hub działa dalej |

Faza statyczna jest mocniejsza, niż się wydaje, i to dzięki specyfikacji: katalog
[`types/`](../lagunbus-spec/types/) mówi, że `Output` ma komendę `Toggle = 0x02`, a `KeyInput`
ma zdarzenie `Click = 0x03`. Literówkę w `Toogle` da się więc złapać **bez dostępu do
sprzętu**, o ile plik deklaruje oczekiwany typ endpointu:

```yaml
bindings:
  - from: kitchen-switch.ep4.Click     # ep4 ma być KeyInput
    to:   kitchen-relay.ep4.Toggle     # ep4 ma być Output
    expect:                            # opcjonalne, ale wtedy walidacja jest pełna
      from: KeyInput
      to:   Output
```

Czy `expect` ma być obowiązkowe — **K2**. Obowiązkowe daje pełną walidację offline kosztem
gadatliwości pliku; opcjonalne odwrotnie.

Rozjazd wykryty w fazie drugiej **nie może być cichy**. Powiązanie wskazujące na endpoint,
którego węzeł nie ma, to zwykle skutek wymiany modułu na inny — i jest to dokładnie ta
sytuacja, w której człowiek ma się dowiedzieć, zamiast zastanawiać się, czemu klawisz
przestał działać.

---

## 11. Zmiana konfiguracji w czasie pracy

Konfigurację zmienia się **głównie z aplikacji**, przez API — edycja pliku w edytorze jest
drogą awaryjną i serwisową, nie podstawową. Plik zostaje mimo to formatem przechowywania,
bo jest czytelny, daje się porównać i daje się wziąć do kopii.

Z tych dwóch zdań wynika wszystko poniżej.

### 11.1 Jedna droga, nie dwie

> **Konfiguracja zawsze idzie przez plik. API zapisuje plik, a nie węzeł.**

```
  aplikacja / API ──┐
                    ├──►  plik (źródło prawdy)  ──►  uzgadnianie (§8)  ──►  węzeł
  edycja ręczna ────┘                │
                                     └──►  Snapshot  ──────────────►  ścieżka szybka, API
```

Kusi, żeby zapis `DebounceTime` z aplikacji poszedł prosto do węzła — jest krótszy i widać
skutek od razu. Byłaby to jednak **druga droga do tej samej rzeczy**, a wtedy plik przestaje
opisywać instalację i zaczyna opisywać to, co ktoś kiedyś w nim napisał. Uzgadnianie (§8)
cofnęłoby zresztą taki zapis przy pierwszym sprawdzeniu zgodności — i miałoby rację.

Sterowanie jest osobną sprawą i idzie wprost: komenda `Toggle` i nastawa robocza (`RW`,`V`)
nie dotykają pliku, bo nie są konfiguracją (§7).

### 11.2 Zapis z API jest check-and-set

Plik ma dwóch piszących — aplikację i człowieka z edytorem — więc potrzebuje tego samego,
czego potrzebuje każdy współdzielony dokument: **wersji**.

```
GET  /config                  →  200, ETag: "sha256:a1b2…"
PUT  /config/nodes/08090a0b0c0d0e0f/endpoints/4/DebounceTime
     If-Match: "sha256:a1b2…"      { "value": 25 }
                              →  200  zapisane, nowy ETag
                              →  409  ktoś zmienił plik w międzyczasie — odśwież i spróbuj
                              →  422  konfiguracja niepoprawna, NIC nie zapisano
```

Ścieżka jest **ścieżką w dokumencie**, więc węzeł występuje w niej pod `Uid`-em, a nie
pod aliasem — tak samo jak w pliku (§4). Alias jest etykietą i wolno go zmienić; adres
zapisu nie może zależeć od czegoś, co da się zmienić.

Hub porównuje skrót pliku z tym, który wydał. Niezgodność znaczy, że ktoś edytował plik
ręcznie albo z drugiej karty przeglądarki — i wtedy **odmowa jest jedyną uczciwą odpowiedzią**.
Cicha wygrana ostatniego zapisu skasowałaby cudzą zmianę bez śladu.

### 11.3 Kolejność zatwierdzania

Kolejność jest ustalona i **nie jest dowolna**:

```
1.  walidacja statyczna nowej treści        (§10)
       nie przeszła → 422, nic się nie zmienia
2.  ZAPIS PLIKU: temp → fsync → rename      niepodzielnie
       nie przeszedł → 500, nic się nie zmienia
3.  przebudowa Snapshot, podmiana referencji
4.  uzgodnienie z węzłami tego, co się zmieniło   (§8)
5.  odpowiedź 200 + nowy ETag
```

Plik zapisuje się **przed** podmianą stanu w pamięci, nie po. Powód jest taki, że z dwóch
możliwych awarii jedna jest znacznie mniej szkodliwa:

| Awaria | Skutek |
|---|---|
| zapisano plik, nie zdążono podmienić | **restart doprowadza do zgodności** |
| podmieniono, nie zapisano pliku | zmiana znika przy restarcie, a nikt o tym nie wie |

Dysk pełny i system plików tylko do odczytu są realnymi awariami na Raspberry Pi. Druga
kolejność sprawiłaby, że konfiguracja działa do najbliższego zaniku zasilania — czyli psuje
się w najgorszym możliwym momencie.

Krok 4 bywa długi: zmiana `ReportInterval` na trzydziestu endpointach to trzydzieści
transakcji. Nie wstrzymuje odpowiedzi — hub potwierdza zapis, a uzgadnianie idzie w tle
i jego postęp widać w `/issues`.

> **Uwaga o wątku CORE.** Krok 2 blokuje go na czas `fsync` — dziesiątki do setek ms na
> karcie SD. Jest to dopuszczalne, bo zmiany konfiguracji są rzadkie, a **ścieżka szybka
> nie przechodzi przez CORE** ([`ARCHITEKTURA.md`](ARCHITEKTURA.md) §3). Opóźnić może
> najwyżej powtórzenie transakcji, które i tak ma sto milisekund budżetu.

### 11.4 Edycja ręczna nadal działa

Skoro plik jest formatem, a nie tylko zrzutem, to zmiana wprowadzona edytorem ma być
podchwycona bez pytania:

```
CORE w swojej pętli terminów (§7.2 architektury) sprawdza co ~1 s
mtime + skrót pliku
        │
   zmiana NIE od huba
        │
        ├── walidacja przeszła  → nowy Snapshot, uzgodnienie, nowy ETag
        └── nie przeszła        → ZOSTAJE STARA, wpis do /issues
```

Odpytywanie co sekundę zamiast `inotify` z jednego powodu: rozwój dzieje się na macOS, gdzie
`inotify` nie istnieje, a jedna operacja `stat` na sekundę jest kosztem, którego nie widać.
`SIGHUP` zostaje jako jawne „przeczytaj teraz".

> **Zła konfiguracja nie ma prawa zatrzymać działającej instalacji.** Przeładowanie, które
> się nie powiodło — z pliku czy z API — zostawia poprzednią w mocy i mówi, co jest nie tak.

### 11.5 Komentarze mają przeżyć zapis

Plik pisany przez maszynę i czytany przez człowieka gubi zwykle to, co czyni go czytelnym.
Zwykły `yaml.dump()` kasuje komentarze, kolejność kluczy i formatowanie — po pierwszym
zapisie z aplikacji plik przestaje być tym, po co był plikiem.

Rozstrzyga to biblioteka: **`ruamel.yaml` w trybie round-trip** zachowuje komentarze,
kolejność i styl cudzysłowów. Jest to jedyny powód, dla którego jej używamy, i wystarczający.

Komentarz przy kluczu usuniętym z aplikacji **znika razem z nim** i tak ma być — komentarz
opisujący nieistniejące ustawienie jest gorszy niż jego brak.

### 11.6 Katalog konfiguracji jako repozytorium git

Nieobowiązkowe, ale prawie za darmo: skoro hub i tak zapisuje plik niepodzielnie, może po
każdym zapisie zrobić commit.

```
config/ jest repozytorium git
  api: rename kitchen-relay ch1 → "Lampa nad stołem"
  api: binding hall-switch.ep4.Click → group(all-lights).Off
  file: ręczna zmiana ReportInterval na 30 endpointach
```

Daje to **historię zmian, autora i cofnięcie** bez pisania ani jednej linijki takiej logiki
w hubie. Wymaga natomiast decyzji, co przy konflikcie i co, gdy ktoś sam zrobi `git pull` —
dlatego jest to **K8**, a nie rozstrzygnięcie.

---

## 12. Czego w konfiguracji nie ma

| Czego nie ma | Gdzie to mieszka |
|---|---|
| adresów węzłów | nadaje je hub, trzyma baza; `Uid` jest tożsamością, adres drogą |
| deskryptorów urządzeń | odczytane z węzłów; hub **nigdy** nie bierze zdolności z katalogu modeli |
| harmonogramów, warunków, pory dnia | Home Assistant — poza zakresem huba i specyfikacji |
| nastaw regulatora | atrybuty węzła; hub zmienia je komendą, nie plikiem |

Pierwszy wiersz zasługuje na uwagę, bo kusi, żeby adres zapisać „dla porządku". Konfiguracja
zapisana na adres rozsypuje się przy pierwszym przenumerowaniu
([`07-hub.md`](../lagunbus-spec/07-hub.md) §2) — a adres w pliku jest przy tym **nieszkodliwy
dopóty, dopóki się zgadza**, czyli jest usterką, która ujawnia się najpóźniej, jak to
możliwe.

---

## 13. Punkty otwarte

| Poz. | Czego dotyczy | Kiedy |
|---|---|---|
| **K4** | czy sceny definiuje się w pliku, czy komendą z zewnątrz | zawartości scen węzeł nie umie odczytać ([`07-hub.md`](../lagunbus-spec/07-hub.md) §2) |
| **K5** | format i zasięg listy usterek konfiguracji | §8, §10 |
| **K6** | domyślny `ReportInterval` w `hub.yaml`, nadpisywany per endpoint | §7.1 |
| **K8** | czy katalog konfiguracji jest repozytorium git i co przy konflikcie | §11.6 |

### 13.0 K1 i K2 — rozstrzygnięte

**K1: cel wskazuje endpoint wprost**, a nie kanał. Skrót przez kanał kusił, ale kanał
może obejmować kilka endpointów — a wtedy „`ch1.Toggle`" przestaje być jednoznaczne
akurat w urządzeniach, dla których kanał w ogóle powstał. Ramka i tak niesie numer
endpointu, więc zapis jest tym, co pojedzie na magistralę.

**K2: `type` jest obowiązkowe**, i to okazało się tańsze, niż wyglądało. Jedno słowo
w każdej stronie powiązania zamienia trzy klasy błędów w błąd przy wczytaniu pliku:
literówkę w nazwie elementu, atrybut, który jest stanem a nie konfiguracją, oraz źródło
postawione na atrybucie nieraportowanym. Wszystkie **bez dostępu do sprzętu**.

### 13.1 K3 i K7 — rozstrzygnięte: dwa kanały, dwie odpowiedzi

Pytanie „czy hub przyjmuje konfigurację z zewnątrz" ma dwie różne odpowiedzi, bo dotyczy
dwóch różnych rzeczy — i wcześniejsze pomieszanie ich było błędem, nie rozstrzygnięciem.

| Kanał | Konfiguruje? | Dlaczego |
|---|---|---|
| **API huba i jego aplikacja** | **tak, i jest to droga główna** | to jest własny interfejs huba, a nie system zewnętrzny |
| **Home Assistant przez MQTT** | **nie** | HA steruje i pokazuje; kierunek hub → HA, jak w `zigbee2mqtt` |

Rozróżnienie jest to samo, które robi `zigbee2mqtt`: jego frontend zapisuje
`configuration.yaml`, a Home Assistant nie. Konfiguracja ma **jedno miejsce i jeden zestaw
reguł spójności** (§11.2, §11.3) — a nie tyle, ile jest podłączonych systemów.

Plik zostaje przy tym źródłem prawdy i formatem przechowywania. Zmieniło się wyłącznie to,
**kto trzyma pióro**: głównie aplikacja, wyjątkowo człowiek z edytorem. Jedno i drugie to
człowiek podejmujący decyzję, więc podział z §1 jest nienaruszony.

### 13.2 Granica między sterowaniem a konfiguracją

Granica nie znika — przesuwa się to, **którą drogą** idzie zapis konfiguracji:

| | Przez API | Jak dociera do węzła |
|---|---|---|
| komenda `Toggle`, `SetLevel` | **tak** | wprost, komendą — nie dotyka pliku |
| nastawa robocza (`RW`, `V`) | **tak** | wprost, zapisem — nie dotyka pliku |
| konfiguracja endpointu (`RW`, `P`) | **tak** | **przez plik**, potem uzgadnianie (§11.1) |
| powiązania, nazwy, źródła | **tak** | przez plik; nie mają odpowiednika w węźle |

> **API zapisuje plik, nigdy węzeł — gdy chodzi o konfigurację.** Sterowanie idzie wprost.

Rozróżnienie biegnie po kolumnach katalogu typów, tak jak w §7: trwałość `P` znaczy
konfigurację, a konfiguracja ma jedną drogę.
