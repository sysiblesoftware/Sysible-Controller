from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QSpinBox, QCheckBox, QLineEdit, QTimeEdit,
)
from PySide6.QtCore import QTime

from client import theme

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# cron's day-of-week field: 0 and 7 both mean Sunday, 1-6 are Mon-Sat.
_CRON_WEEKDAY_NUM = {"Mon": "1", "Tue": "2", "Wed": "3", "Thu": "4", "Fri": "5", "Sat": "6", "Sun": "0"}

# systemd OnCalendar's day-of-week field takes the same three-letter
# abbreviations this widget already uses, so no translation needed there.


class HumanScheduleBuilder(QWidget):
    """Plain-English schedule picker that replaces hand-written cron
    syntax (`*/15 * * * *`) or systemd OnCalendar syntax
    (`*-*-* 02:00:00`) with a frequency dropdown plus whichever simple
    controls that frequency needs (an interval, a time of day, days of
    the week, a day of the month). value() returns the right string
    for whichever backend asked for it - call to_cron() or
    to_oncalendar() directly if a caller specifically needs one or the
    other regardless of mode.

    mode="cron" adds an "At system boot" frequency (cron's @reboot
    shortcut) that mode="calendar" leaves out, since systemd timers
    already have a dedicated OnBootSec field elsewhere on the Create
    Timer panel for that case - OnCalendar has no boot-time concept of
    its own.

    An "Advanced" checkbox swaps the whole builder for a single raw
    text field, so a schedule the builder can't express (specific
    weekdays mixed with a monthly day-of-month, multiple times a day,
    etc.) is still always reachable - the friendly controls are the
    default path, not the only path.
    """

    def __init__(self, mode="cron", parent=None):
        super().__init__(parent)
        self.mode = mode

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.builder_container = QWidget()
        layout = QVBoxLayout(self.builder_container)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.builder_container)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Run:"))
        self.frequency = QComboBox()
        options = ["Every N minutes", "Every N hours", "Daily", "Weekly", "Monthly"]
        if mode == "cron":
            options.append("At system boot")
        self.frequency.addItems(options)
        self.frequency.currentTextChanged.connect(self._on_frequency_changed)
        freq_row.addWidget(self.frequency)
        freq_row.addStretch()
        layout.addLayout(freq_row)

        # -- Every N minutes --
        self.minutes_row = QWidget()
        mr = QHBoxLayout(self.minutes_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(QLabel("Every"))
        self.every_n_minutes = QSpinBox()
        self.every_n_minutes.setRange(1, 59)
        self.every_n_minutes.setValue(15)
        mr.addWidget(self.every_n_minutes)
        mr.addWidget(QLabel("minute(s)"))
        mr.addStretch()
        layout.addWidget(self.minutes_row)

        # -- Every N hours --
        self.hours_row = QWidget()
        hr = QHBoxLayout(self.hours_row)
        hr.setContentsMargins(0, 0, 0, 0)
        hr.addWidget(QLabel("Every"))
        self.every_n_hours = QSpinBox()
        self.every_n_hours.setRange(1, 23)
        self.every_n_hours.setValue(1)
        hr.addWidget(self.every_n_hours)
        hr.addWidget(QLabel("hour(s), at minute"))
        self.hours_at_minute = QSpinBox()
        self.hours_at_minute.setRange(0, 59)
        hr.addWidget(self.hours_at_minute)
        hr.addStretch()
        layout.addWidget(self.hours_row)

        # -- Time of day (Daily / Weekly / Monthly all need this) --
        self.time_row = QWidget()
        tr = QHBoxLayout(self.time_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.addWidget(QLabel("At"))
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("h:mm AP")
        self.time_edit.setTime(QTime(2, 0))
        tr.addWidget(self.time_edit)
        tr.addStretch()
        layout.addWidget(self.time_row)

        # -- Weekly: which day(s) --
        self.weekday_row = QWidget()
        wr = QHBoxLayout(self.weekday_row)
        wr.setContentsMargins(0, 0, 0, 0)
        wr.addWidget(QLabel("On:"))
        self.weekday_checks = {}
        for day in WEEKDAYS:
            cb = QCheckBox(day)
            if day == "Mon":
                cb.setChecked(True)
            cb.toggled.connect(self._update_summary)
            self.weekday_checks[day] = cb
            wr.addWidget(cb)
        wr.addStretch()
        layout.addWidget(self.weekday_row)

        # -- Monthly: which day of the month --
        self.month_day_row = QWidget()
        mdr = QHBoxLayout(self.month_day_row)
        mdr.setContentsMargins(0, 0, 0, 0)
        mdr.addWidget(QLabel("On day"))
        self.month_day = QSpinBox()
        self.month_day.setRange(1, 28)
        self.month_day.setValue(1)
        mdr.addWidget(self.month_day)
        mdr.addWidget(QLabel("of the month"))
        mdr.addStretch()
        layout.addWidget(self.month_day_row)

        # -- live plain-English + raw-string preview --
        self.summary_label = QLabel()
        theme.style_hint_label(self.summary_label)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        for w in (self.every_n_minutes, self.every_n_hours, self.hours_at_minute, self.month_day):
            w.valueChanged.connect(self._update_summary)
        self.time_edit.timeChanged.connect(self._update_summary)

        # -- advanced escape hatch --
        self.advanced_toggle = QCheckBox("Advanced: enter raw schedule manually")
        self.advanced_toggle.toggled.connect(self._on_advanced_toggled)
        outer.addWidget(self.advanced_toggle)

        self.advanced_input = QLineEdit()
        self.advanced_input.setPlaceholderText(
            "e.g. */15 * * * *, or @daily" if mode == "cron" else "e.g. *-*-* 02:00:00"
        )
        self.advanced_input.setMaximumWidth(300)
        self.advanced_input.setVisible(False)
        outer.addWidget(self.advanced_input)

        self._on_frequency_changed(self.frequency.currentText())

    # =========================================================
    # VISIBILITY
    # =========================================================
    def _on_frequency_changed(self, freq):
        self.minutes_row.setVisible(freq == "Every N minutes")
        self.hours_row.setVisible(freq == "Every N hours")
        self.time_row.setVisible(freq in ("Daily", "Weekly", "Monthly"))
        self.weekday_row.setVisible(freq == "Weekly")
        self.month_day_row.setVisible(freq == "Monthly")
        self._update_summary()

    def _on_advanced_toggled(self, checked):
        self.builder_container.setVisible(not checked)
        self.advanced_input.setVisible(checked)

    # =========================================================
    # SUMMARY / DESCRIPTION
    # =========================================================
    def _selected_weekdays(self):
        days = [d for d in WEEKDAYS if self.weekday_checks[d].isChecked()]
        return days or ["Mon"]  # a frequency with zero days picked would build an invalid schedule

    def describe(self):
        """One-line plain-English description of the current selection,
        e.g. 'every day at 2:00 AM' or 'every Mon, Wed, Fri at 9:00 PM'."""
        freq = self.frequency.currentText()
        if freq == "Every N minutes":
            n = self.every_n_minutes.value()
            return "every minute" if n == 1 else f"every {n} minutes"
        if freq == "Every N hours":
            n = self.every_n_hours.value()
            m = self.hours_at_minute.value()
            base = "every hour" if n == 1 else f"every {n} hours"
            return f"{base}, at :{m:02d} past the hour" if m else base
        t = self.time_edit.time().toString("h:mm AP")
        if freq == "Daily":
            return f"every day at {t}"
        if freq == "Weekly":
            return f"every {', '.join(self._selected_weekdays())} at {t}"
        if freq == "Monthly":
            return f"on day {self.month_day.value()} of the month at {t}"
        if freq == "At system boot":
            return "at system boot"
        return ""

    def _update_summary(self, *args):
        raw = self.to_cron() if self.mode == "cron" else self.to_oncalendar()
        self.summary_label.setText(f"Runs {self.describe()}   ({raw})")

    # =========================================================
    # OUTPUT
    # =========================================================
    def to_cron(self):
        """5-field cron string, or '@reboot', built from the current
        selections - independent of self.mode, so callers can ask for
        this specifically if they ever need to."""
        freq = self.frequency.currentText()
        if freq == "At system boot":
            return "@reboot"
        if freq == "Every N minutes":
            return f"*/{self.every_n_minutes.value()} * * * *"
        if freq == "Every N hours":
            return f"{self.hours_at_minute.value()} */{self.every_n_hours.value()} * * *"
        t = self.time_edit.time()
        minute, hour = t.minute(), t.hour()
        if freq == "Daily":
            return f"{minute} {hour} * * *"
        if freq == "Weekly":
            days = ",".join(_CRON_WEEKDAY_NUM[d] for d in self._selected_weekdays())
            return f"{minute} {hour} * * {days}"
        if freq == "Monthly":
            return f"{minute} {hour} {self.month_day.value()} * *"
        return ""

    def to_oncalendar(self):
        """systemd OnCalendar= string built from the current selections.
        No boot-time option here by design - see the class docstring."""
        freq = self.frequency.currentText()
        if freq == "Every N minutes":
            return f"*-*-* *:0/{self.every_n_minutes.value()}:00"
        if freq == "Every N hours":
            return f"*-*-* 0/{self.every_n_hours.value()}:{self.hours_at_minute.value():02d}:00"
        t = self.time_edit.time()
        hm = f"{t.hour():02d}:{t.minute():02d}:00"
        if freq == "Daily":
            return f"*-*-* {hm}"
        if freq == "Weekly":
            days = ",".join(self._selected_weekdays())
            return f"{days} *-*-* {hm}"
        if freq == "Monthly":
            return f"*-*-{self.month_day.value():02d} {hm}"
        return ""

    def value(self):
        """The string to actually hand to the backend - the Advanced
        field verbatim if that's switched on, otherwise whichever of
        to_cron()/to_oncalendar() matches self.mode."""
        if self.advanced_toggle.isChecked():
            return self.advanced_input.text().strip()
        return self.to_cron() if self.mode == "cron" else self.to_oncalendar()
