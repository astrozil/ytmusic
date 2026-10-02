import pytest
import requests
from billboard import BillboardNotFoundException, BillboardParseException
from bs4 import BeautifulSoup
from unittest.mock import Mock

from services.billboard_chart import BillboardChart


@pytest.mark.parametrize("options,path", [
    ({}, "hot-100"),
    ({"date": "2026-10-03"}, "hot-100/2026-10-03"),
    ({"year": "2025"}, "year-end/2025/hot-100"),
])
def test_chart_fetch_uses_shared_transport_and_preserves_url(options, path):
    response = Mock(status_code=200, text=labelled_chart())
    fetch = Mock(return_value=response)
    chart = BillboardChart("hot-100", fetch=False, http_get=fetch,
                           timeout=4, max_retries=2, **options)
    chart._parsePage = Mock(wraps=chart._parsePage if not options.get("year") else lambda soup: None)
    chart.fetchEntries()
    fetch.assert_called_once_with(f"https://www.billboard.com/charts/{path}", timeout=4, retries=2)
    chart._parsePage.assert_called_once()
    response.close.assert_called_once()
    if not options.get("year"):
        assert chart[0].title == "A & B"


@pytest.mark.parametrize("status,error", [(404, BillboardNotFoundException), (403, requests.HTTPError)])
def test_chart_fetch_preserves_http_errors_and_closes_response(status, error):
    response = Mock(status_code=status)
    response.raise_for_status.side_effect = requests.HTTPError("Forbidden")
    fetch = Mock(return_value=response)
    with pytest.raises(error):
        BillboardChart("hot-100", http_get=fetch, timeout=1, max_retries=0)
    fetch.assert_called_once()
    response.close.assert_called_once()


def test_chart_parser_failure_is_not_retried_and_closes_response():
    response = Mock(status_code=200, text="<html>Invalid chart</html>")
    fetch = Mock(return_value=response)
    with pytest.raises(BillboardParseException):
        BillboardChart("hot-100", http_get=fetch, timeout=1, max_retries=3)
    fetch.assert_called_once()
    response.close.assert_called_once()


def labelled_chart(last="-", weeks="7"):
    # Matches the nested desktop/mobile statistics on the current Hot 100 page.
    def stat(label, value):
        return f'<span class="c-span">{label}</span><ul><li><span class="c-label">{value}</span></li></ul>'

    desktop = "".join(stat(label, value) for label, value in [
        ("LW", last), ("PEAK", "1"), ("WEEKS AT NO. 1", "3"),
        ("WEEKS ON CHART", weeks),
    ])
    mobile = "".join(stat(label, value) for label, value in [
        ("WEEKS ON CHART", weeks), ("PEAK", "1"), ("LW", last),
    ])
    return f'''<div id="chart-date-picker" data-date="2026-10-03"></div>
    <ul class="o-chart-results-list-row">
      <li><span class="c-label">1</span></li><li><img src="https://example.com/art.jpg"></li><li></li>
      <li><ul><li><h3 id="title-of-a-story">A &amp; B</h3><span class="c-label">Artist</span></li>
        <li></li><li>{desktop}</li><li></li><li><ul><li>{mobile}</li></ul></li>
      </ul></li>
    </ul>'''


@pytest.mark.parametrize("last,weeks,is_new", [("-", "7", False), ("-", "1", True), ("4", "9", False)])
def test_labelled_statistics_preserve_chart_metadata(last, weeks, is_new):
    chart = BillboardChart("hot-100", fetch=False)
    chart._parseNewStylePage(BeautifulSoup(labelled_chart(last, weeks), "html.parser"))
    assert chart.date == "2026-10-03"
    assert len(chart) == 1
    entry = chart[0]
    assert (entry.title, entry.artist, entry.rank) == ("A & B", "Artist", 1)
    assert entry.image == "https://example.com/art.jpg"
    assert (entry.lastPos, entry.peakPos, entry.weeks, entry.isNew) == (
        0 if last == "-" else int(last), 1, int(weeks), is_new,
    )


@pytest.mark.parametrize("html", [
    "<html><body>Unavailable</body></html>",
    labelled_chart().replace("WEEKS ON CHART", "UNKNOWN"),
    labelled_chart(weeks="unexpected"),
    labelled_chart().replace('<span class="c-label">1</span></li><li><img', '<span class="c-label">2</span></li><li><img'),
])
def test_changed_or_invalid_pages_raise_instead_of_caching_bad_data(html):
    chart = BillboardChart("hot-100", fetch=False)
    with pytest.raises(BillboardParseException):
        chart._parseNewStylePage(BeautifulSoup(html, "html.parser"))
    assert chart.entries == []


def test_legacy_positional_layout_still_parses():
    html = '''<div id="chart-date-picker" data-date="2024-01-06"></div>
    <ul class="o-chart-results-list-row">
      <li><span class="c-label">1</span></li><li></li><li></li><li><ul>
      <li><h3 id="title-of-a-story">Song</h3><span class="c-label">Artist</span></li>
      <li></li><li>2</li><li>1</li><li>8</li>
      </ul></li>
    </ul>'''
    chart = BillboardChart("hot-100", fetch=False)
    chart._parseNewStylePage(BeautifulSoup(html, "html.parser"))
    assert (chart[0].rank, chart[0].lastPos, chart[0].peakPos, chart[0].weeks) == (1, 2, 1, 8)
