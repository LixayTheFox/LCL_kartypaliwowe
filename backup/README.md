# Fuel Insight

Desktopowa aplikacja do analizy jednego raportu paliwowego XLSX i tworzenia
rankingu kierowców lub pojazdów.

## Uruchomienie

Na Windows kliknij dwukrotnie:

`uruchom_aplikacje.bat`

Przy pierwszym uruchomieniu skrypt utworzy lokalne środowisko `.venv` i
zainstaluje bibliotekę `openpyxl`. Wymagany jest Python 3.

Alternatywnie:

```powershell
py -3 -m venv .venv
..venvScriptspython.exe -m pip install -r requirements.txt
..venvScriptspython.exe app.py
```

## Jak działa analiza

- Do programu wybierany jest jeden raport XLSX.
- Numery rejestracyjne są normalizowane, więc np. `BI 228HA` i `BI228HA`
  oznaczają ten sam pojazd.
- Diesel/ON z raportu jest sumowany. AdBlue jest raportowane osobno.
- Dystans to różnica między największym i najmniejszym przebiegiem dla pojazdu
  w analizowanym okresie.
- Szacowane spalanie jest liczone jako `diesel razem / dystans * 100`.
- Najgorszy wynik to najwyższe spalanie spośród pozycji, które osiągnęły
  minimalny dystans ustawiony w aplikacji.
- Jeśli raport nie zawiera nazwisk, wynik jest pokazany per pojazd. Nazwisko
  można przypisać w dolnym panelu, a przypisanie zostanie zapamiętane.

## Archiwum

Każdy poprawnie wczytany raport jest automatycznie zapisywany lokalnie w
`%APPDATA%\FuelInsight\archiwum`. Zapisane raporty można później wczytać z
panelu `Archiwum` bez ponownego wybierania pliku XLSX.

## Eksport

Eksport XLSX zawiera:

- arkusz `Podsumowanie` z rankingiem i wyróżnionym najgorszym wynikiem,
- arkusz `Transakcje` ze szczegółami wczytanego raportu,
- arkusz `Mapowanie kierowców`.

Dostępny jest także prosty eksport CSV.
