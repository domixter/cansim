# cansim

**Symulator magistrali CAN dla firmware STM32, z interfejsem w przeglądarce.**

Układasz schemat magistrali z płytek z Twoim kodem C, sterowników ODrive, komputera z adapterem USB-CAN i terminatorów.
Każda płytka wykonuje swój prawdziwy, skompilowany kod. Widzisz rejestry kontrolera CAN, jego pamięć, ramki i ich
przepływ między węzłami, możesz ręcznie sterować symulacją z konsoli, a reguły elektryczne i czasowe (ERC) wskazują
błędy schematu i konfiguracji, zanim cokolwiek trafi na sprzęt.

Obsługiwane kontrolery: **STM32H7 FDCAN** (klasyczny CAN) i **STM32F4 bxCAN**.

## Możliwości

- **Twój kod C bez zmian**: wiele plików `.c/.h`, bare metal (rejestry przez nagłówki CMSIS). Kod jest kompilowany
  `gcc` do biblioteki i uruchamiany w wirtualnym czasie; odwołania do rejestrów trafiają do modelu sprzętu.
- **Wiele płytek na jednej magistrali**, każda z własnym kodem (np. H7 i F4 rozmawiające ze sobą).
- **Schemat**: CANH, CANL, GND, rezystory 120 Ω, transceivery, długość kabla i odgałęzień; elementy przeciągasz myszką.
- **Przepływ ramek**: kto nadał ramkę, kto ją przyjął (filtr), a kto tylko potwierdził ACK.
- **Rejestry** z polami bitowymi, aktywnością (odczyty i zapisy) i flagami, które ustawiły się na chwilę.
- **Pamięć kontrolera**: filtry, bufory, FIFO (FDCAN Message RAM, skrzynki i banki filtrów bxCAN).
- **Zmienne globalne jak w debuggerze**: podgląd na żywo, zmiana wartości, lista obserwowanych.
- **Wywoływanie funkcji C** z listy, z typami argumentów odczytanymi z kodu.
- **Konsola komend z podpowiedziami** i **scenariusze testowe** z asercjami (`oczekuj ...` daje OK albo BŁĄD).
- **Wstrzykiwanie awarii**: zepsute ramki, wymuszony TEC (Error Passive, Bus-Off), utrata zasilania węzła, zły bitrate,
  wyciszony heartbeat, brak terminacji, brak masy.
- **ERC** z odnośnikami do norm (ISO 11898-1/-2, CiA) i **kontrola kodu** z miejscem w kodzie (`plik.c:linia`).
- **Narzędzia**: kalkulator bit timingu (gotowa linia C dla `NBTP` albo `BTR`), kalkulator obciążenia magistrali.
- **Eksport** logu ramek w formacie `candump` i CSV.
- Interfejs po polsku, motyw jasny i ciemny, objaśnienia w programie i słowniczek pojęć.

## Instalacja i uruchomienie

### Windows
1. **Python 3.9+**: <https://www.python.org/downloads/>, w instalatorze zaznacz **„Add python.exe to PATH”**.
2. **gcc (64-bit)**: z <https://winlibs.com/> pobierz wersję **Win64, UCRT, zip** (bez LLVM), rozpakuj np. do
   `C:\mingw64` i dodaj `C:\mingw64\bin` do zmiennej `PATH`.
3. Pobierz repozytorium (Code → Download ZIP albo `git clone`) i kliknij dwa razy **`start.bat`**.
4. Przy pierwszym uruchomieniu program zaproponuje pobranie oficjalnych nagłówków ST
   (albo uruchom `pobierz_naglowki_st.bat`; potrzebny internet, git nie jest wymagany).

### Linux / macOS
```
sudo apt install python3 gcc git          # macOS: xcode-select --install
git clone <adres repozytorium> cansim && cd cansim
python3 tools/fetch_cmsis.py              # oficjalne nagłówki ST do third_party/
./start.sh                                # albo: python3 -m cansim
```

Program działa tylko lokalnie (serwer na `127.0.0.1`) i otwiera się w przeglądarce. Nie wymaga dodatkowych pakietów
Pythona (tylko biblioteka standardowa). Opcje: `python -m cansim [plik.json] [--port 9000] [--no-browser]`.

## Pierwsze kroki
1. Na ekranie startowym wybierz **Płytka z moim kodem** (folder z plikami `.c/.h`; pliki źródłowe i katalogi nagłówków
   wykryją się same) albo **Nowa płytka od zera** (powstanie folder z działającym szablonem `main.c` dla H7 albo F4).
2. Dodaj resztę magistrali przyciskami po lewej: **ODrive**, **Węzeł** (np. PC z USB-CAN), **Terminator 120 Ω**.
3. **▶ Start** (spacja) albo **+1 / +10 / +100 ms**. Pasek pod menu pokazuje, co się dzieje; przycisk **?** objaśnia
   bieżącą zakładkę, a **Pomoc** zawiera przewodnik i słowniczek.
4. **Zapisz** (Ctrl+S) zapisuje cały schemat do `cansim_system.json`.

Ustawienia płytki (pliki `.c`, katalogi nagłówków, zegary, podmiana funkcji opóźniających, obserwowane zmienne) są
w prawym panelu po kliknięciu płytki, w sekcji **Ustawienia kompilacji płytki**. Twoje pliki nie są modyfikowane:
kopia trafia do folderu `.cansim_build/` obok kodu.

## Konsola komend i scenariusze
Konsola jest na dole okna. `Tab` podpowiada komendy i argumenty (płytki, funkcje i zmienne z typami), strzałki
przywołują historię. Płytki podaje się po nazwie bez spacji (np. `Moja_plytka`), ODrive po numerze `node_id`.

```
pomoc                                 lista komend
czekaj 500                            przesuń symulację o 500 ms
stan                                  podsumowanie wszystkich węzłów
odrive 1 predkosc 2.5                 zadana prędkość ODrive 1 (odrive * ... = wszystkie)
odrive 2 stan closed_loop             inne akcje: blad, kasuj_blad, zasilanie on|off, heartbeat on|off, watchdog, bitrate
wyslij PC 0x2D 00 00 20 40            wyślij ramkę z węzła generycznego
wywolaj Moja_plytka moja_funkcja 1 2  wywołaj funkcję C w płytce
ustaw Moja_plytka licznik 100         zapisz zmienną globalną (pokaz = odczyt, obserwuj = do zakładki Zmienne)
awaria uszkodz 5                      5 zepsutych ramek; awaria tec <płytka> 260 = wymuś Bus-Off
ramki 0x2D 5                          ostatnie ramki o ID 0x2D
oczekuj Moja_plytka rx_frames > 10    asercja: OK albo BŁĄD
```

**Scenariusz testowy** (zakładka obok konsoli) to lista komend wykonywanych po kolei z wynikiem ✓/✗ dla każdej linii.
Zapisuje się w schemacie, więc można go powtarzać po każdej zmianie kodu.

## Plik schematu
`cansim_system.json` opisuje cały system: `boards` (płytki z konfiguracją kompilacji), `nodes` (`odrive`, `generic`,
`terminator`), `bus` (długość kabla), opcjonalnie `messages` (definicje sygnałów do dekodowania ramek: nazwa, bit
startowy, długość, ze znakiem, skala, jednostka) i `scenarios`. Plik tworzy i aktualizuje program; ręczna edycja
nie jest potrzebna.

## Co sprawdza ERC
| Reguła | Źródło |
|---|---|
| 2 × 120 Ω na końcach linii (60 Ω między CANH a CANL), terminator w środku linii | ISO 11898-2 |
| wspólna masa GND każdego węzła | ISO 11898-2 (zakres napięcia wspólnego odbiornika) |
| zakres zasilania transceivera (TJA1042, TJA1051, MCP2562: 4,5–5,5 V; SN65HVD230: 3,0–3,6 V) | karty katalogowe |
| ten sam bitrate wszystkich węzłów | ISO 11898-1 |
| opóźnienie propagacji przy danej długości kabla ≤ (TSEG1 − SJW) | Bosch CAN 2.0 / ISO 11898-1 |
| tolerancja oscylatora: df ≤ min(PS1, PS2) / (2(13·NBT − PS2)), df ≤ SJW / (20·NBT) | ISO 11898-1 |
| punkt próbkowania 75–90 % (zalecane 87,5 %), SJW ≤ TSEG2 | CiA 301, ISO 11898-1 |
| długość kabla i odgałęzień względem prędkości | ISO 11898-2, reguły praktyczne CiA |
| unikalność identyfikatorów (wykrywana z ruchu), obciążenie > 70 %, samotny węzeł, Bus-Off | ISO 11898-1 |

Kontrola kodu podczas symulacji (z `plik:linia`): zapis rejestru chronionego FDCAN bez `INIT` i `CCE`, ustawienie
`CCE` przed utrwaleniem `INIT`, `SJW > TSEG2`, nakładające się obszary Message RAM, `ANFS = 0`, potwierdzenie pustego
elementu FIFO; bxCAN: zapis `BTR` poza inicjalizacją, `FINIT` zostawiony na 1, brak aktywnych filtrów, zapis do
zajętej skrzynki, `TXRQ` w inicjalizacji; „burza przerwań” (flaga niekasowana w ISR).

## Zgodność z dokumentacją (wybrane)
- **FDCAN** (RM0433, Bosch M_CAN): rejestry chronione tylko przy `INIT = 1` i `CCE = 1`; wartości po resecie; Bus-Off
  ustawia `INIT`, powrót po programowym `INIT = 0` i 129 × 11 bitach recesywnych; `PSR.LEC` = 7 po odczycie i 0 po
  poprawnej ramce; bufor Rx z ustawionym `NDAT` jest zablokowany (filtr „nie pasuje”, filtrowanie idzie dalej); FIFO0
  w trybie blokującym i nadpisującym; `TXBAR`, `TXBRP`, `TXBTO`, `TXBCF`, `CCCR.DAR`.
- **bxCAN** (RM0090): tryby sleep, init i normal; `BTR` tylko przy `INAK = 1`; 3 skrzynki z priorytetem po ID albo
  `TXFP`; `TSR.CODE` i `LOW`; FIFO po 3 ramki z `FOVR` i `RFLM`; filtry maska/lista 16/32-bit z priorytetem i numeracją
  FMI niezależną od aktywacji banku; `FINIT = 1` wyłącza odbiór; `ABOM`, `NART`.
- **Protokół** (ISO 11898-1): arbitraż, długość ramki z CRC15 i bit stuffingiem, ACK, TEC +8 / REC +1, wyjątek ACK
  w Error Passive, progi 96/128/255, wstrzymanie nadawania w Error Passive, kolizja dwóch nadawców z tym samym ID,
  węzeł o innym bitrate.

## Ograniczenia
- To model, nie pomiar: nazwy i bity rejestrów pochodzą z oficjalnych nagłówków ST, ale zachowanie peryferiów jest
  odtworzone z dokumentacji. Przed wdrożeniem sprawdź na sprzęcie (analizator CAN, oscyloskop).
- Fizyka linii jest sprawdzana regułami ERC, nie symulowana napięciowo. Jedyny efekt fizyczny w symulacji: brak
  jakiejkolwiek terminacji psuje każdą ramkę (założenie modelu).
- Czas wirtualny jest przybliżony; zegary nie są emulowane (wartości podaje się w ustawieniach płytki).
- Pominięte: CAN FD (faza danych), filtry rozszerzone, FIFO1 i Tx FIFO/queue w FDCAN, tryby silent/loopback i
  Time Triggered, przegrana arbitrażu (ALST), DMA, wejście UART; timery i UART są atrapami.
- Dostęp do peryferium przez wskaźnik (`p = FDCAN1; p->IR`) i `&FDCAN1->REG` nie jest przechwytywany.
  Funkcje i zmienne `static` nie są dostępne z konsoli (nie ma ich w tablicy symboli biblioteki).
- Model ODrive jest uproszczony (CANSimple 0.5.x, ID = `node_id << 5 | cmd`).
- Testowane głównie na Linuxie; Windows (MinGW-w64) i macOS mogą wymagać poprawek.

## Struktura
```
cansim/        kod programu
  regs.py      parser nagłówków ST (rejestry, pola, przerwania)
  rewrite.py   instrumentacja źródeł (#line zachowuje numery linii)
  build.py     kompilacja płytki (gcc -shared), konfiguracja projektu
  engine.py    jedna płytka: wątki firmware, NVIC, SysTick
  world.py     wspólny czas i magistrala wielu płytek
  bus.py       magistrala i protokół CAN
  models/      FDCAN, bxCAN, atrapy RCC/USART/timerów
  nodes/       ODrive, węzeł generyczny
  system.py    schemat i ERC
  console.py   konsola komend i scenariusze
  symbols.py   funkcje i zmienne z kodu (dla konsoli i interfejsu)
  templates/   szablony main.c dla nowej płytki (H7, F4)
  app.py, server.py, web/index.html   interfejs
tools/         pobieranie nagłówków ST (fetch_cmsis.py, pobierz_naglowki_st.ps1)
start.bat, start.sh, pobierz_naglowki_st.bat   uruchamianie
```
