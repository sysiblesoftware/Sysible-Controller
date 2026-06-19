from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame

from client import theme
from client.branding import make_page_header
from version import VERSION


class VersionLicensingPage(QWidget):
    """Version & Licensing - what version of Sysible Controller this
    install is running, plus a Licensing section.

    Licensing is a placeholder for now (no licensing model exists yet)
    - just a status row saying so, rather than leaving the tile empty
    or hiding it until that's built. Replace _build_licensing_section
    once there's an actual license to check/show.
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Version & Licensing")
        self.resize(460, 420)

        outer = QVBoxLayout()
        self.setLayout(outer)

        outer.addLayout(make_page_header("Version & Licensing", font_size=22, logo_height=32))
        outer.addSpacing(8)

        self._build_version_section(outer)
        outer.addWidget(self._divider())
        self._build_licensing_section(outer)

        outer.addStretch()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    @staticmethod
    def _section_label(text):
        label = QLabel(text)
        label.setStyleSheet("font-size:16px;font-weight:bold;")
        return label

    @staticmethod
    def _info_row(label_text, value_text):
        row = QHBoxLayout()

        label = QLabel(label_text)
        theme.style_hint_label(label)
        row.addWidget(label)

        value = QLabel(value_text)
        value.setStyleSheet("font-weight:bold;")
        row.addWidget(value)

        row.addStretch()
        return row

    # =========================================================
    # SECTION 1: VERSION
    # =========================================================
    def _build_version_section(self, layout):
        layout.addWidget(self._section_label("Version"))
        layout.addLayout(self._info_row("Sysible Controller:", f"v{VERSION}"))

    # =========================================================
    # SECTION 2: LICENSING (placeholder)
    # =========================================================
    def _build_licensing_section(self, layout):
        layout.addWidget(self._section_label("Licensing"))
        layout.addLayout(self._info_row("Status:", "No licensing model configured yet"))

        note = QLabel(
            "Licensing is not yet enforced on this build - this section is a "
            "placeholder reserved for license key entry and seat/entitlement "
            "status once that's built out."
        )
        theme.style_hint_label(note)
        note.setWordWrap(True)
        layout.addWidget(note)
