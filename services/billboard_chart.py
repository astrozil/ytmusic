"""Compatibility parser for Billboard's labelled chart statistics."""

from billboard import BillboardParseException, ChartData, ChartEntry


class BillboardChart(ChartData):
    def _parseNewStylePage(self, soup):
        rows = soup.select("ul.o-chart-results-list-row")
        # Older pages still use the positional layout supported by billboard.py.
        if rows and not rows[0].select("span.c-span"):
            return super()._parseNewStylePage(soup)
        if not rows:
            raise BillboardParseException("No Billboard chart rows found")

        date_element = soup.select_one("#chart-date-picker")
        if date_element:
            self.date = date_element.get("data-date")
        self.previousDate = self.nextDate = None

        entries = []
        for row in rows:
            title_element = row.select_one("#title-of-a-story")
            artist_element = row.select_one("#title-of-a-story + span.c-label")
            cells = row.find_all("li", recursive=False)
            rank_element = cells[0].select_one("span.c-label") if cells else None
            if not title_element or not artist_element or not rank_element:
                raise BillboardParseException("Missing Billboard title, artist or rank")

            # Desktop/mobile repeat the same fields. Labels avoid confusing
            # 'weeks at no. 1' with 'weeks on chart' or counting nested cells.
            metadata = {}
            for label in row.select("span.c-span"):
                name = label.get_text(" ", strip=True).upper()
                if name not in {"LW", "PEAK", "WEEKS ON CHART"}:
                    continue
                value_list = label.find_next_sibling("ul")
                value_element = value_list.select_one("span.c-label") if value_list else None
                if value_element:
                    value = value_element.get_text(strip=True)
                    if name in metadata and metadata[name] != value:
                        raise BillboardParseException(f"Conflicting Billboard statistic: {name}")
                    metadata[name] = value

            def number(name, absent_value=None):
                value = metadata.get(name)
                if value is None:
                    raise BillboardParseException(f"Missing Billboard statistic: {name}")
                if value == "-":
                    return absent_value
                try:
                    return int(value)
                except ValueError as exc:
                    raise BillboardParseException(f"Invalid Billboard statistic: {name}") from exc

            try:
                rank = int(rank_element.get_text(strip=True))
            except ValueError as exc:
                raise BillboardParseException("Invalid Billboard rank") from exc
            peak = number("PEAK")
            last = number("LW", 0)
            weeks = number("WEEKS ON CHART", 1)
            image_element = row.select_one("img")
            image = (image_element.get("data-lazy-src") or image_element.get("src")) if image_element else None
            entries.append(ChartEntry(
                title_element.get_text(" ", strip=True),
                artist_element.get_text(" ", strip=True),
                image, peak, last, weeks, rank, weeks == 1,
            ))

        ranks = [entry.rank for entry in entries]
        if sorted(ranks) != list(range(1, len(entries) + 1)):
            raise BillboardParseException("Billboard ranks are incomplete or duplicated")
        self.entries.extend(entries)
