"""Draw a chat's activity as one image: a panel per metric over a shared date axis, with dated events below.

matplotlib is an optional dependency and is imported only when a figure is drawn.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
import io
import math
from pathlib import Path
import sys
import tempfile
import textwrap
import types
import unicodedata
import warnings

from .analysis import BUCKETS, aggregate, totals


EVENT_STYLES = ("auto", "list", "key")
MAX_PEOPLE = 6
NEED_MATPLOTLIB = 'The figure needs matplotlib: python -m pip install "voxpad[plot]"'
# One colour per person drawn, in this order. Neighbours stay apart for colour-blind readers as well.
COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
EVENT_COLOR = "#5b5f97"
EVENT_SIZE = 8.0
KEY_SIZE = 7.6
TITLE_SIZE = 11
HATCH = "/////"
STYLE = {
    "font.family": "DejaVu Sans", "axes.edgecolor": "#cccccc", "axes.linewidth": 0.8, "figure.facecolor": "white",
    "axes.facecolor": "white", "hatch.linewidth": 0.6,
}
# The layout is fixed in inches, so that panels keep their height however many there are and however long the events run.
ROW = 2.35
TOP = 0.95
PANEL_GAP = 0.45
STRIP_GAP = 0.40
DATE_ROOM = 0.95
TITLE_LINE = TITLE_SIZE * 1.3 / 72
LEGEND_LINE = 0.27
LEFT, SHARE = 0.06, 0.925
# Agg cannot hold an image with a side of 65536 pixels or more.
MAX_PIXELS = 60000


def require_matplotlib():
    """Return the parts of matplotlib the figure uses, or say how to install it."""
    try:
        import matplotlib
        from matplotlib import dates, patches, patheffects, ticker
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as error:
        raise RuntimeError(NEED_MATPLOTLIB) from error
    # No pyplot: it keeps every figure alive and belongs to one thread, and the desktop application draws from a worker.
    return types.SimpleNamespace(matplotlib=matplotlib, dates=dates, patches=patches, patheffects=patheffects, ticker=ticker,
                                 Canvas=FigureCanvasAgg, Figure=Figure)


def chat_span(model: dict) -> tuple[date, date] | None:
    """Return the first and the last day that has a message, or None when no message has a date."""
    days = []
    for key in {message["time"][:10] for message in model["messages"] if isinstance(message.get("time"), str)}:
        try:
            days.append(date.fromisoformat(key))
        except ValueError:
            continue
    return (min(days), max(days)) if days else None


def split_events(events: Sequence[dict], span: tuple[date, date] | None) -> tuple[list[dict], list[dict]]:
    """Separate the events to draw from those far outside the chat: (near, far).

    The date axis is stretched to show an event near the chat's edges. One further
    away than 21 days, or than 15 % of the chat's length when that is more, would
    squeeze the chat itself and is left out.
    """
    if span is None:
        return [], list(events)
    first, last = span
    reach = max(21, (last - first).days * 0.15)
    near, far = [], []
    for event in events:
        day = date.fromisoformat(event["date"])
        (near if (first - day).days <= reach and (day - last).days <= reach else far).append(event)
    return near, far


def choose_people(model: dict, names: Sequence[str] = ()) -> tuple[list[int], str | None]:
    """Return the participants to draw, as places in `participants`, and a note when others are left out.

    `names` must name participants exactly. Without names the two who sent the
    most messages are drawn.
    """
    participants = model["participants"]
    if not participants:
        raise ValueError("Nothing to plot: the chat has no message with a sender.")
    if names:
        places = {unicodedata.normalize("NFC", name): place for place, name in enumerate(participants)}
        wanted = list(dict.fromkeys(unicodedata.normalize("NFC", name) for name in names))
        unknown = [name for name in wanted if name not in places]
        if unknown:
            raise ValueError(f"Nobody in this chat is called {' or '.join(unknown)}. Its participants are: {', '.join(participants)}")
        if len(wanted) > MAX_PEOPLE:
            raise ValueError(f"The figure draws at most {MAX_PEOPLE} people.")
        return [places[name] for name in wanted], None
    chosen = list(range(min(2, len(participants))))
    if len(participants) <= 2:
        return chosen, None
    others = sum(row["messages"] for row in totals(model)[2:])
    return chosen, (f"{len(participants)} people wrote in this chat; the figure draws the two most active, {participants[0]} and "
                    f"{participants[1]}. The others sent {others:,} messages. Choose who is drawn with --person.")


def _plain(text: str) -> str:
    """A text as matplotlib draws it literally: between two dollar signs it would read a formula."""
    return text.replace("\t", " ").replace("$", "\\$")


def _count(value) -> str:
    return f"{int(round(value)):,}"


def _tint(colour: str) -> tuple[float, float, float]:
    """A colour mixed with white, for the part of a bar that is hatched."""
    return tuple(1 - (1 - int(colour[place:place + 2], 16) / 255) * 0.4 for place in (1, 3, 5))


def _bucket_end(start: date, bucket: str) -> date:
    """The last day of the bucket that starts on `start`."""
    if bucket == "week":
        return start + timedelta(days=6)
    if bucket == "month":
        return date(start.year + start.month // 12, start.month % 12 + 1, 1) - timedelta(days=1)
    return start


def _panels(model: dict, chosen: list[int], table: dict, notify) -> list[dict]:
    """What each panel shows for the people drawn. A metric nobody has anything of gets no panel."""
    sums = totals(model)
    names = [model["participants"][place] for place in chosen]

    def total(metric: str) -> list:
        return [sums[place][metric] for place in chosen]

    def series(metric: str, scale: float = 1) -> list[list]:
        return [[value * scale for value in table["series"][place][metric]] for place in chosen]

    def entries(texts) -> list[str]:
        return [f"{name}: {text}" for name, text in zip(names, texts)]

    notes, transcribed, timed = sum(total("voice_notes")), sum(total("voice_transcribed")), sum(total("voice_timed"))
    typed, spoken = total("words_typed"), total("words_spoken")
    minutes = [seconds / 60 for seconds in total("voice_seconds")]
    words = {"noun": "words", "label": "Words", "empty": not sum(typed) + sum(spoken), "layers": [(series("words_typed"), False)],
             "entries": entries(map(_count, typed)), "note": None, "counted": True}
    if notes:
        # Spoken words stand on the typed ones. A chat transcribed in part must not read as one that said little.
        words["layers"].append((series("words_spoken"), True))
        words["entries"] = entries(f"{_count(wrote)} typed + {_count(said)} spoken" for wrote, said in zip(typed, spoken))
        if transcribed < notes:
            words["note"] = f"({transcribed:,} of {notes:,} voice notes transcribed)"
    panels = [
        {"noun": "messages", "label": "Messages", "empty": not sum(total("messages")), "layers": [(series("messages"), False)],
         "entries": entries(map(_count, total("messages"))), "note": None, "counted": True},
        words,
        {"noun": "voice notes", "label": "Voice notes", "empty": not notes, "layers": [(series("voice_notes"), False)],
         "entries": entries(map(_count, total("voice_notes"))), "note": None, "counted": True},
        {"noun": "minutes of audio", "label": "Minutes", "empty": not sum(minutes), "layers": [(series("voice_seconds", 1 / 60), False)],
         "entries": entries(f"{value:,.1f} min" for value in minutes),
         "note": f"({timed:,} of {notes:,} timed)" if timed < notes else None, "counted": False},
    ]
    for panel in panels:
        if panel["empty"]:
            notify(f"[skip] no data for {panel['noun']}.")
    return [panel for panel in panels if not panel["empty"]]


def _measurer(lib):
    """Return measure(text, size) -> (width, height) in inches, taken on a figure that is never saved."""
    scratch = lib.Figure(figsize=(4, 1))
    lib.Canvas(scratch)
    renderer = scratch.canvas.get_renderer()

    def measure(text: str, size: float, **options) -> tuple[float, float]:
        artist = scratch.text(0, 0, text, fontsize=size, **options)
        box = artist.get_window_extent(renderer)
        artist.remove()
        return box.width / scratch.dpi, box.height / scratch.dpi

    return measure


def _title_lines(head: str, entries: list[str], note: str | None, fits) -> list[str]:
    """A panel's title, the totals carried over to further lines where one line is too short for them."""
    pieces = [(entry, "   ·   ") for entry in entries] + ([(note, "    ")] if note else [])
    lines, line = [], head
    for position, (piece, gap) in enumerate(pieces):
        joined = line + ("    " if position == 0 else gap) + piece
        if position and not fits(joined):
            lines.append(line)
            joined = piece
        line = joined
    return lines + [line]


def _legend_rows(entries: list[dict], room: float, measure) -> list[list[dict]]:
    """Set the legend's entries in rows no wider than `room` inches; each entry learns its width."""
    rows, used = [[]], 0.0
    for entry in entries:
        entry["width"] = 0.30 + measure(entry["label"], TITLE_SIZE)[0]
        if rows[-1] and used + 0.30 + entry["width"] > room:
            rows.append([])
            used = 0.0
        used += (0.30 if rows[-1] else 0) + entry["width"]
        rows[-1].append(entry)
    return rows


def _plan_list(events: list[tuple], width: float, limits: tuple[float, float], measure, stamp: str) -> dict:
    """Place every event's text under its date, in inches: wrapped, measured, and moved down until it touches no other."""
    low, high = limits
    reach = max(1e-9, high - low)
    columns = max(24, int(min(3.6, max(1.8, width * 0.15)) / 0.060))
    offset = 6 / 72
    items, placed, bottom = [], [], 0.0
    for _, mark, day, label in events:
        lines = textwrap.wrap(f"{day.strftime(stamp)} · {label}", columns) or [day.strftime(stamp)]
        text = _plain("\n".join(lines))
        # The drawn size, not a guess from the number of lines: a long note is taller than its lines times the font size.
        wide, tall = measure(text, EVENT_SIZE, linespacing=1.1)
        share = (mark - low) / reach
        right = share > 0.62
        x = share * width * SHARE
        start, end = (x - offset - wide, x - offset) if right else (x + offset, x + offset + wide)
        y, moved = 0.02, True
        while moved:
            moved = False
            for other_start, other_end, top, base in placed:
                if not (end <= other_start or other_end <= start) and not (y + tall <= top or base <= y):
                    y, moved = base + 0.06, True
        placed.append((start, end, y, y + tall))
        items.append({"x": mark, "text": text, "right": right, "y": y, "line": tall / len(lines), "box": placed[-1]})
        bottom = max(bottom, y + tall)
    return {"items": items, "height": bottom + 0.05}


def _draw_list(lib, axes: list, strip, plan: dict) -> None:
    """Draw the planned list. The strip's vertical unit is the inch, counted from its top, so the plan maps onto it as it is."""
    strip.set_yticks([])
    for spine in ("top", "left", "right"):
        strip.spines[spine].set_visible(False)
    strip.set_ylim(plan["height"], 0)
    # Lines to later events cross the text of earlier ones; a white edge keeps the letters whole.
    halo = [lib.patheffects.withStroke(linewidth=2.4, foreground="white")]
    for item in plan["items"]:
        x, top = item["x"], item["y"]
        dot = top + item["line"] * 0.5
        for ax in axes:
            ax.axvline(x, color=EVENT_COLOR, ls=(0, (3, 4)), lw=0.7, alpha=0.20, zorder=0.6)
        strip.plot([x, x], [0, dot], color=EVENT_COLOR, ls=(0, (3, 3)), lw=0.8, alpha=0.45, clip_on=False, zorder=1.5)
        strip.plot([x], [dot], marker="o", ms=4, color=EVENT_COLOR, clip_on=False, zorder=3)
        strip.annotate(item["text"], xy=(x, top), xytext=(-6 if item["right"] else 6, 0), textcoords="offset points", va="top",
                       ha="right" if item["right"] else "left", linespacing=1.1, fontsize=EVENT_SIZE, color=EVENT_COLOR,
                       clip_on=False, zorder=3, path_effects=halo)


def _plan_key(events: list[tuple], width: float, limits: tuple[float, float], measure) -> dict:
    """Plan numbered marks on the date axis and the numbered notes below them, in columns that fit the axis."""
    room = width * SHARE
    count = max(1, min(3, int(room / 7.5)))
    column = room / count - 0.3
    budget = max(24, int(column / 0.058))
    while True:
        entries = [textwrap.wrap(f"{number}. {day.strftime('%d %b %Y')} — {label}", budget) for number, _, day, label in events]
        widest = max(measure(_plain(line), KEY_SIZE)[0] for lines in entries for line in lines)
        if widest <= column or budget <= 24:
            break
        budget = max(24, min(budget - 1, int(budget * column / widest)))
    share = math.ceil(sum(map(len, entries)) / count)
    columns, used = [[]], 0
    for lines in entries:
        if used >= share and len(columns) < count:
            columns.append([])
            used = 0
        columns[-1].append(lines)
        used += len(lines)
    rows = max(sum(map(len, lines)) for lines in columns)
    # A mark is as wide as its disc or its number, whichever is wider; marks that would touch go to a lane further down.
    per_inch = (limits[1] - limits[0]) / room
    lanes, ends = [], []
    for number, mark, _, _ in events:
        wide = (max(9 / 72, measure(str(number), EVENT_SIZE)[0]) + 0.03) * per_inch
        lane = next((lane for lane, end in enumerate(ends) if mark - wide / 2 >= end), len(ends))
        if lane == len(ends):
            ends.append(0.0)
        ends[lane] = mark + wide / 2
        lanes.append(lane)
    return {"columns": columns, "rows": rows, "legend": 0.4 + rows * 0.17, "lanes": lanes, "strip": max(0.7, 0.32 + (len(ends) - 1) * 0.17)}


def _draw_key(lib, axes: list, strip, legend, events: list[tuple], plan: dict) -> None:
    strip.set_ylim(0, 1)
    strip.set_yticks([])
    for spine in ("top", "left", "right"):
        strip.spines[spine].set_visible(False)
    across = strip.get_xaxis_transform()
    for (number, mark, _, _), lane in zip(events, plan["lanes"]):
        y = 0.5 if max(plan["lanes"]) == 0 else 1 - (0.16 + lane * 0.17) / plan["strip"]
        for ax in axes:
            ax.axvline(mark, color=EVENT_COLOR, ls=(0, (4, 3)), lw=1.0, alpha=0.5, zorder=0.8)
        strip.plot([mark, mark], [1.0, y], transform=across, color=EVENT_COLOR, ls=(0, (4, 3)), lw=0.8, alpha=0.7, clip_on=False)
        strip.plot([mark], [y], marker="o", ms=9, color=EVENT_COLOR, transform=across, clip_on=False, zorder=3)
        strip.text(mark, y, str(number), transform=across, color="white", fontsize=6.3 if number < 100 else 5.2, fontweight="bold",
                   ha="center", va="center", zorder=4, clip_on=False)
    legend.set_xlim(0, 1)
    legend.set_ylim(0, 1)
    legend.axis("off")
    step = 1.0 / (plan["rows"] + 0.5)
    for place, column in enumerate(plan["columns"]):
        x, y = 0.004 + place / len(plan["columns"]), 0.98
        for lines in column:
            for position, line in enumerate(lines):
                legend.text(x, y, _plain(line), fontsize=KEY_SIZE, va="top", ha="left", color="#333333" if position else EVENT_COLOR)
                y -= step


def _date_axis(lib, axis, days: float) -> None:
    """Label the days so that single ones can be told: every day or few in a short chat, Mondays, then months."""
    dates = lib.dates
    if days <= 45:
        major, minor, pattern = dates.DayLocator(interval=max(1, int(days) // 15)), dates.DayLocator(), "%a %d %b"
    elif days <= 250:
        major, minor, pattern = dates.WeekdayLocator(byweekday=dates.MO), dates.DayLocator(), "%a %d %b"
    else:
        major, minor, pattern = dates.MonthLocator(), dates.WeekdayLocator(byweekday=dates.MO), "%b %Y"
    axis.xaxis.set_major_locator(major)
    axis.xaxis.set_major_formatter(dates.DateFormatter(pattern))
    axis.xaxis.set_minor_locator(minor)
    axis.grid(True, which="major", axis="x", color="#c4c4c4", lw=0.7, alpha=0.9)
    if days <= 45:
        axis.grid(True, which="minor", axis="x", color="#ededed", lw=0.5)


def _draw_panel(lib, axis, panel: dict, centres: list[float], lengths: list[int], unit: str, widest: float) -> None:
    """Draw one metric: for every bucket a bar per person, side by side, the layers of a bar on top of each other.

    `widest` is the most a bar may take of the date axis: a chat of three days
    would otherwise be drawn as three slabs.
    """
    people = len(panel["layers"][0][0])
    for person in range(people):
        widths = [min(0.84 * length / people, widest) for length in lengths]
        places = [centre + (person - (people - 1) / 2) * wide for centre, wide in zip(centres, widths)]
        base = [0] * len(places)
        for values, hatched in panel["layers"]:
            look = ({"facecolor": _tint(COLORS[person]), "edgecolor": COLORS[person], "hatch": HATCH} if hatched
                    else {"color": COLORS[person]})
            axis.bar(places, values[person], width=widths, bottom=base, linewidth=0, zorder=3, **look)
            base = [low + value for low, value in zip(base, values[person])]
    axis.xaxis_date()
    axis.set_axisbelow(True)
    axis.set_ylabel(f"{panel['label']}\nper {unit}", fontsize=10.5)
    axis.set_ylim(bottom=0)
    if panel["counted"]:
        # Half a message or half a voice note does not exist.
        axis.yaxis.set_major_locator(lib.ticker.MaxNLocator(integer=True))
    axis.grid(True, axis="y", color="#e2e2e2", lw=0.6)
    axis.set_title("\n".join(panel["title"]), fontsize=TITLE_SIZE, loc="left", pad=6)
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)


def _compose(lib, names: list[str], panels: list[dict], starts: list[date], bucket: str, events: list[tuple], style: str,
             span: tuple[date, date]):
    """Build the figure: the heading, the panels, the events and the date axis. Returns it with its size in inches."""
    number = lambda day: lib.dates.date2num(datetime(day.year, day.month, day.day))
    ends = [_bucket_end(start, bucket) for start in starts]
    lengths = [(end - start).days + 1 for start, end in zip(starts, ends)]
    # A day is drawn around its own tick; a week or a month around the middle of its days.
    centres = [number(start) + (length - 1) / 2 for start, length in zip(starts, lengths)]
    events = [(position, number(day), day, label) for position, day, label in events]
    marks = [mark for _, mark, _, _ in events]
    low, high = min([number(starts[0])] + marks), max([number(ends[-1])] + marks)
    days = high - low
    width = min(46.0, max(14.0, 2.5 + days * 0.14))
    pad = days * 0.01 + 0.5
    limits = (low - pad, high + pad)
    room = width * SHARE
    measure = _measurer(lib)
    years = {lib.dates.num2date(low).year, lib.dates.num2date(high).year}

    for panel in panels:
        panel["title"] = _title_lines(panel["noun"].capitalize(), [_plain(entry) for entry in panel["entries"]], panel["note"],
                                      lambda text: measure(text, TITLE_SIZE)[0] <= room)
    who = names[0] if len(names) == 1 else " vs ".join(names) if len(names) == 2 else f"{len(names)} people"
    period = f"({span[0].isoformat()} – {span[1].isoformat()})"
    heading = _plain(f"WhatsApp activity by {bucket}: {who}   {period}")
    if measure(heading, 14)[0] > room:
        heading = f"WhatsApp activity by {bucket}   {period}"
    legend = [{"label": _plain(name), "colour": COLORS[place], "hatched": False} for place, name in enumerate(names)]
    if any(hatched for panel in panels for _, hatched in panel["layers"]):
        legend.append({"label": "hatched: words spoken in voice notes", "colour": "#8a8a8a", "hatched": True})
    rows = _legend_rows(legend, room, measure)
    # Beside the heading when both fit on its line, as two people do; otherwise on lines of its own below it.
    beside = len(rows) == 1 and measure(heading, 14)[0] + 0.5 + sum(entry["width"] for entry in rows[0]) + 0.3 * (len(rows[0]) - 1) <= room
    top = TOP + (0 if beside else len(rows) * LEGEND_LINE)

    plan = None
    if events and style == "key":
        plan = _plan_key(events, width, limits, measure)
        below, bottom = STRIP_GAP + plan["strip"] + DATE_ROOM + plan["legend"], 0.25
    elif events:
        plan = _plan_list(events, width, limits, measure, "%a %d %b %Y" if len(years) > 1 else "%a %d %b")
        below, bottom = STRIP_GAP + plan["height"], DATE_ROOM
    else:
        below, bottom = 0.0, DATE_ROOM
    gaps = [(len(panel["title"]) - 1) * TITLE_LINE + (PANEL_GAP if place else 0) for place, panel in enumerate(panels)]
    height = top + sum(gaps) + len(panels) * ROW + below + bottom
    figure = lib.Figure(figsize=(width, height))
    lib.Canvas(figure)

    def box(down: float, tall: float) -> list[float]:
        return [LEFT, 1.0 - (down + tall) / height, SHARE, tall / height]

    axes, cursor = [], top
    for panel, gap in zip(panels, gaps):
        cursor += gap
        axes.append(figure.add_axes(box(cursor, ROW), sharex=axes[0] if axes else None))
        cursor += ROW
    strip = notes = None
    if plan and style == "key":
        strip = figure.add_axes(box(cursor + STRIP_GAP, plan["strip"]), sharex=axes[0])
        notes = figure.add_axes(box(cursor + STRIP_GAP + plan["strip"] + DATE_ROOM, plan["legend"]))
    elif plan:
        strip = figure.add_axes(box(cursor + STRIP_GAP, plan["height"]), sharex=axes[0])
    dated = strip or axes[-1]

    for axis, panel in zip(axes, panels):
        _draw_panel(lib, axis, panel, centres, lengths, bucket, 0.45 * (limits[1] - limits[0]) / room)
    _date_axis(lib, dated, days)
    dated.set_xlabel("Date", fontsize=11)
    for axis in axes:
        axis.grid(True, axis="x", which="major", color="#ececec", lw=0.6)
        if axis is not dated:
            axis.tick_params(labelbottom=False)
    for label in dated.get_xticklabels():
        label.set_rotation(45)
        label.set_horizontalalignment("right")
    axes[0].set_xlim(*limits)
    # Weekends give the days their rhythm; in a long chat, or one counted by week or month, the bands would only be noise.
    if bucket == "day" and days <= 250:
        day = date.fromordinal(lib.dates.num2date(limits[0]).toordinal())
        while number(day) <= limits[1]:
            if day.weekday() >= 5:
                for axis in axes:
                    axis.axvspan(number(day) - 0.5, number(day) + 0.5, color="#eceef4", lw=0, zorder=0)
            day += timedelta(days=1)
    if plan and style == "key":
        _draw_key(lib, axes, strip, notes, events, plan)
    elif plan:
        _draw_list(lib, axes, strip, plan)

    figure.text(LEFT, 1.0 - 0.34 / height, heading, fontsize=14, ha="left", va="top")
    for place, row in enumerate(rows):
        down = 0.46 if beside else 0.34 + 0.36 + place * LEGEND_LINE
        across = LEFT * width + (room - sum(entry["width"] for entry in row) - 0.3 * (len(row) - 1) if beside else 0)
        for entry in row:
            look = ({"facecolor": "#f1f1f1", "edgecolor": entry["colour"], "hatch": HATCH} if entry["hatched"]
                    else {"facecolor": entry["colour"]})
            figure.add_artist(lib.patches.Rectangle((across / width, 1.0 - (down + 0.07) / height), 0.22 / width, 0.14 / height,
                                                    transform=figure.transFigure, linewidth=0, **look))
            figure.text((across + 0.30) / width, 1.0 - down / height, entry["label"], fontsize=TITLE_SIZE, ha="left", va="center")
            across += entry["width"] + 0.3
    return figure, width, height


def _write_bytes(path: Path, data: bytes) -> None:
    """Replace an image only after its new contents have been written in full."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_figure(path: Path, model: dict, *, bucket: str = "day", people: Sequence[str] = (), event_style: str = "auto",
                 notify=None) -> dict:
    """Draw a conversation model as a PNG: messages, words, voice notes and minutes of audio per day, week or month.

    `people` names the participants to draw, at most six; without it the two most
    active are drawn. The model's events are drawn below the panels as a list of
    full texts (`list`, which `auto` means too) or as numbered marks with a key
    (`key`), numbered as the viewer numbers them. `notify(text)` receives the
    notes: who is drawn, and the panels left out for lack of data. Returns
    {"path", "people", "panels", "titles", "events", "far_events", "size"}: the
    names drawn, the nouns of the panels and the titles written above them, the
    events drawn and left out, and (width, height) in inches.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"Unknown bucket: {bucket}")
    if event_style not in EVENT_STYLES:
        raise ValueError(f"Unknown event style: {event_style}")
    notify = notify or (lambda text: print(text, file=sys.stderr))
    chosen, note = choose_people(model, people)
    lib = require_matplotlib()
    table = aggregate(model, bucket)
    span = chat_span(model)
    if not table["buckets"] or span is None:
        raise ValueError("Nothing to plot: no message of this chat has a date that can be read.")
    if note:
        notify(note)
    panels = _panels(model, chosen, table, notify)
    if not panels:
        raise ValueError("Nothing to plot.")
    ordered = sorted(model["events"], key=lambda event: event["date"])
    near, far = split_events(ordered, span)
    kept = {id(event) for event in near}
    # An event keeps the number the viewer gives it, also when an earlier one is left out here.
    events = [(position, date.fromisoformat(event["date"]), event["label"]) for position, event in enumerate(ordered, 1) if id(event) in kept]
    names = [model["participants"][place] for place in chosen]
    starts = [date.fromisoformat(key) for key in table["buckets"]]
    with warnings.catch_warnings(), lib.matplotlib.rc_context(STYLE):
        # The font has no emoji and few scripts; a name is then drawn with boxes, which needs no warning per letter.
        warnings.filterwarnings("ignore", message="Glyph .* missing from", category=UserWarning)
        figure, width, height = _compose(lib, names, panels, starts, bucket, events, "key" if event_style == "key" else "list", span)
        image = io.BytesIO()
        figure.savefig(image, format="png", dpi=min(150, int(MAX_PIXELS / max(width, height))), bbox_inches="tight", pad_inches=0.25)
    _write_bytes(Path(path), image.getvalue())
    return {"path": Path(path), "people": names, "panels": [panel["noun"] for panel in panels],
            "titles": ["\n".join(panel["title"]) for panel in panels], "events": len(near), "far_events": len(far),
            "size": (float(width), float(height))}
