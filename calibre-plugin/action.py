#
# Copyright (C) 2023 github.com/ping
#
# This file is part of the OverDrive Libby Plugin by ping
# OverDrive Libby Plugin for calibre / libby-calibre-plugin
#
# See https://github.com/ping/libby-calibre-plugin for more
# information
#

from timeit import default_timer as timer
from typing import Dict

from calibre import browser
from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import Dispatcher
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.ebook_download import show_download_info
from calibre.gui2.threaded_jobs import ThreadedJob
from polyglot.builtins import as_unicode
from qt.core import (
    Qt,
    QToolButton,
    QDialog,
    QGridLayout,
    QPushButton,
    QCheckBox,
    QAbstractItemView,
    QTableView,
    QHeaderView,
    QSortFilterProxyModel,
    QAbstractTableModel,
    QThread,
    QObject,
    pyqtSignal,
    QStatusBar,
    QSize,
    QErrorMessage,
    QMenu,
    QCursor,
    QModelIndex,
    QUrl,
    QDesktopServices,
)

from . import logger, PLUGIN_NAME, PLUGIN_ICON, __version__
from .config import PREFS, PreferenceKeys, PreferenceTexts
from .ebook_download import CustomEbookDownload
from .libby import LibbyClient
from .libby.client import LibbyFormats, LibbyMediaTypes
from .loan_return import LibbyLoanReturn
from .magazine_download import CustomMagazineDownload
from .magazine_download_utils import parse_datetime

load_translations()


class OverdriveLibbyAction(InterfaceAction):
    name = PLUGIN_NAME
    action_spec = (
        "OverDrive Libby",
        None,
        _("Run the OverDrive Libby client UI"),
        None,
    )
    popup_type = QToolButton.MenuButtonPopup
    action_type = "current"
    dont_add_to = frozenset(["context-menu-device"])

    def genesis(self):
        # This method is called once per plugin, do initial setup here
        icon = get_icons(PLUGIN_ICON, "OverDrive Libby Plugin")
        self.qaction.setIcon(icon)
        self.qaction.triggered.connect(self.show_dialog)

    def show_dialog(self):
        base_plugin_object = self.interface_action_base_plugin
        do_user_config = base_plugin_object.do_user_config
        d = OverdriveLibbyDialog(self.gui, self.qaction.icon(), do_user_config)
        d.show()

    def apply_settings(self):
        pass


gui_ebook_download = CustomEbookDownload()
gui_magazine_download = CustomMagazineDownload()
guid_libby_return = LibbyLoanReturn()


class DataWorker(QObject):
    finished = pyqtSignal(dict)

    def __int__(self):
        super().__init__()

    def run(self):
        libby_token = PREFS[PreferenceKeys.LIBBY_TOKEN]
        if not libby_token:
            self.finished.emit([])

        start = timer()
        client = LibbyClient(
            identity_token=libby_token, max_retries=1, timeout=30, logger=logger
        )
        synced_state = client.sync()
        logger.info("Request took %f seconds" % (timer() - start))
        self.finished.emit(synced_state)


def get_loan_title(loan: Dict) -> str:
    title = loan["title"]
    if loan["type"]["id"] == LibbyMediaTypes.Magazine:
        title = f'{loan["title"]} - {loan.get("edition", "")}'
    return title


class OverdriveLibbyDialog(QDialog):
    def __init__(self, gui, icon, do_user_config):
        super().__init__(gui)
        self.gui = gui
        self.do_user_config = do_user_config
        self.db = gui.current_db.new_api
        self.client = None
        self.__thread = QThread()
        self.__curr_width = 0
        self.__curr_height = 0

        libby_token = PREFS[PreferenceKeys.LIBBY_TOKEN]
        if libby_token:
            self.client = LibbyClient(
                identity_token=libby_token, max_retries=1, timeout=30, logger=logger
            )

        self.layout = QGridLayout()
        self.setLayout(self.layout)
        self.setWindowTitle(
            _("OverDrive Libby v%s") % ".".join([str(d) for d in __version__])
        )
        self.setWindowIcon(icon)
        loan_view_span = 8

        self.refresh_btn = QPushButton("\u21BB " + _("Refresh"), self)
        self.refresh_btn.setAutoDefault(False)
        self.refresh_btn.setToolTip(_("Get latest loans"))
        self.refresh_btn.clicked.connect(self.do_refresh)
        self.layout.addWidget(self.refresh_btn, 0, 0)

        self.status_bar = QStatusBar(self)
        self.status_bar.setSizeGripEnabled(False)
        self.layout.addWidget(self.status_bar, 0, 1, 1, 3)

        self.model = LibbyLoansModel(None, [], self.db)
        self.search_proxy_model = QSortFilterProxyModel(self)
        self.search_proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.search_proxy_model.setFilterKeyColumn(-1)
        self.search_proxy_model.setSourceModel(self.model)

        # The main loan list
        self.loans_view = QTableView(self)
        self.loans_view.setSortingEnabled(True)
        self.loans_view.setAlternatingRowColors(True)
        self.loans_view.setMinimumWidth(720)
        self.loans_view.setModel(self.search_proxy_model)
        horizontal_header = self.loans_view.horizontalHeader()
        for col_index, mode in [
            (0, QHeaderView.ResizeMode.Stretch),
            (1, QHeaderView.ResizeMode.ResizeToContents),
            (2, QHeaderView.ResizeMode.ResizeToContents),
            (3, QHeaderView.ResizeMode.ResizeToContents),
            (4, QHeaderView.ResizeMode.ResizeToContents),
        ]:
            horizontal_header.setSectionResizeMode(col_index, mode)
        self.loans_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.loans_view.sortByColumn(-1, Qt.AscendingOrder)
        # add context menu
        self.loans_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.loans_view.customContextMenuRequested.connect(self.loan_view_context_menu)
        self.layout.addWidget(self.loans_view, 1, 0, 3, loan_view_span)

        self.download_btn = QPushButton("\u2913 " + _("Download"), self)
        self.download_btn.setAutoDefault(False)
        self.download_btn.setToolTip(_("Download selected loans"))
        self.download_btn.setStyleSheet("padding: 4px 16px")
        self.download_btn.clicked.connect(self.download_selected_loans)
        self.layout.addWidget(self.download_btn, 5, loan_view_span - 1)

        self.hide_book_already_in_lib_checkbox = QCheckBox(
            PreferenceTexts.HIDE_BOOKS_ALREADY_IN_LIB, self
        )
        self.hide_book_already_in_lib_checkbox.clicked.connect(
            self.set_hide_books_already_in_library
        )
        self.hide_book_already_in_lib_checkbox.setChecked(
            PREFS[PreferenceKeys.HIDE_BOOKS_ALREADY_IN_LIB]
        )
        self.layout.addWidget(self.hide_book_already_in_lib_checkbox, 5, 0, 1, 3)

        if (
            PREFS[PreferenceKeys.MAIN_UI_WIDTH]
            and PREFS[PreferenceKeys.MAIN_UI_WIDTH] > 0
            and PREFS[PreferenceKeys.MAIN_UI_HEIGHT]
            and PREFS[PreferenceKeys.MAIN_UI_HEIGHT] > 0
        ):
            logger.debug(
                "Resizing window using saved preferences: (%d, %d)",
                PREFS[PreferenceKeys.MAIN_UI_WIDTH],
                PREFS[PreferenceKeys.MAIN_UI_HEIGHT],
            )
            self.resize(
                QSize(
                    PREFS[PreferenceKeys.MAIN_UI_WIDTH],
                    PREFS[PreferenceKeys.MAIN_UI_HEIGHT],
                )
            )
        else:
            self.resize(self.sizeHint())

        # for pseudo-debouncing resizeEvent
        self.__curr_width = self.size().width()
        self.__curr_height = self.size().height()

        self.fetch_loans()

    def resizeEvent(self, e):
        # Because resizeEvent is called *multiple* times during a resize,
        # we will save the new window size only when the differential is
        # greater than min_diff.
        # This does not completely debounce the saves, but it does reduce
        # it reasonably imo.
        new_size = e.size()
        new_width = new_size.width()
        new_height = new_size.height()
        min_diff = 5
        if (
            new_width
            and new_width > 0
            and abs(new_width - self.__curr_width) >= min_diff
            and new_width != PREFS[PreferenceKeys.MAIN_UI_WIDTH]
        ):
            PREFS[PreferenceKeys.MAIN_UI_WIDTH] = new_width
            self.__curr_width = new_width
            logger.debug("Saved new UI width preference: %d", new_width)
        if (
            new_height
            and new_height > 0
            and abs(new_height - self.__curr_height) >= min_diff
            and new_height != PREFS[PreferenceKeys.MAIN_UI_HEIGHT]
        ):
            PREFS[PreferenceKeys.MAIN_UI_HEIGHT] = new_height
            self.__curr_height = new_height
            logger.debug("Saved new UI height preference: %d", new_height)

    def do_refresh(self):
        self.model.refresh_loans({})
        self.fetch_loans()

    def fetch_loans(self):
        if not self.__thread.isRunning():
            self.refresh_btn.setEnabled(False)
            self.status_bar.showMessage(_("Fetching loans..."))
            self.__thread = self.__get_thread()
            self.__thread.start()

    def __get_thread(self):
        thread = QThread()
        worker = DataWorker()
        worker.moveToThread(thread)
        thread.worker = worker
        thread.started.connect(worker.run)

        def loaded(value):
            self.model.refresh_loans(value)
            self.refresh_btn.setEnabled(True)
            self.status_bar.clearMessage()
            thread.quit()

        worker.finished.connect(lambda value: loaded(value))

        return thread

    def set_hide_books_already_in_library(self, checked):
        PREFS[PreferenceKeys.HIDE_BOOKS_ALREADY_IN_LIB] = checked
        self.model.set_filter_hide_books_already_in_library(checked)
        self.loans_view.sortByColumn(-1, Qt.AscendingOrder)

    def download_selected_loans(self):
        selection_model = self.loans_view.selectionModel()
        if selection_model.hasSelection():
            rows = selection_model.selectedRows()
            for row in reversed(rows):
                self.download_loan(row.data(Qt.UserRole))
        else:
            d = QErrorMessage(self)
            d.showMessage(_("Please select at least 1 loan."), "select_at_least_1_loan")

    def download_loan(self, loan):
        format_id = LibbyClient.get_loan_format(
            loan, prefer_open_format=PREFS[PreferenceKeys.PREFER_OPEN_FORMATS]
        )
        if LibbyClient.is_downloadable_ebook_loan(loan):
            show_download_info(get_loan_title(loan), self)
            tags = [t.strip() for t in PREFS[PreferenceKeys.TAG_EBOOKS].split(",")]
            if format_id in (LibbyFormats.EBookEPubOpen, LibbyFormats.EBookPDFOpen):
                # special handling required for these formats
                self.download_ebook(
                    loan,
                    format_id,
                    filename=f'{loan["id"]}.{LibbyClient.get_file_extension(format_id)}',
                    tags=tags,
                )
            else:
                endpoint_url, headers = self.client.get_loan_fulfilment_details(
                    loan["id"], loan["cardId"], format_id
                )

                def create_custom_browser():
                    br = browser()
                    for k, v in headers.items():
                        br.set_header(k, v)
                    return br

                self.gui.download_ebook(
                    url=endpoint_url,
                    create_browser=create_custom_browser,
                    tags=tags,
                )

        if LibbyClient.is_downloadable_magazine_loan(loan):
            show_download_info(get_loan_title(loan), self)
            tags = [t.strip() for t in PREFS[PreferenceKeys.TAG_MAGAZINES].split(",")]
            self.download_magazine(
                loan,
                format_id,
                filename=f'{loan["id"]}.{LibbyClient.get_file_extension(format_id)}',
                tags=tags,
            )

    def download_ebook(
        self,
        loan: Dict,
        format_id: str,
        url="",
        cookie_file=None,
        filename="",
        save_loc="",
        add_to_lib=True,
        tags=[],
        create_browser=None,
    ):
        # We will handle the downloading of the files ourselves instead of depending
        # on the calibre browser

        # Heavily referenced from
        # https://github.com/kovidgoyal/calibre/blob/58c609fa7db3a8df59981c3bf73823fa1862c392/src/calibre/gui2/ebook_download.py#L127-L152

        description = _("Downloading %s") % as_unicode(
            f'"{get_loan_title(loan)}"', errors="replace"
        )
        callback = Dispatcher(self.gui.downloaded_ebook)
        job = ThreadedJob(
            "overdrive_libby_download",
            description,
            gui_ebook_download,
            (
                self.gui,
                self.client,
                loan,
                format_id,
                cookie_file,
                url,
                filename,
                save_loc,
                add_to_lib,
                tags,
                create_browser,
            ),
            {},
            callback,
            max_concurrent_count=1,
            killable=False,
        )
        self.gui.job_manager.run_threaded_job(job)
        self.gui.status_bar.show_message(description, 3000)

    def download_magazine(
        self,
        loan: Dict,
        format_id: str,
        url="",
        cookie_file=None,
        filename="",
        save_loc="",
        add_to_lib=True,
        tags=[],
        create_browser=None,
    ):
        # We will handle the downloading of the files ourselves instead of depending
        # on the calibre browser

        # Heavily referenced from
        # https://github.com/kovidgoyal/calibre/blob/58c609fa7db3a8df59981c3bf73823fa1862c392/src/calibre/gui2/ebook_download.py#L127-L152

        description = _("Downloading %s") % as_unicode(
            f'"{get_loan_title(loan)}"', errors="replace"
        )
        callback = Dispatcher(self.gui.downloaded_ebook)
        job = ThreadedJob(
            "overdrive_libby_download",
            description,
            gui_magazine_download,
            (
                self.gui,
                self.client,
                loan,
                format_id,
                cookie_file,
                url,
                filename,
                save_loc,
                add_to_lib,
                tags,
                create_browser,
            ),
            {},
            callback,
            max_concurrent_count=1,
            killable=True,
        )
        self.gui.job_manager.run_threaded_job(job)
        self.gui.status_bar.show_message(description, 3000)

    def loan_view_context_menu(self, pos):
        selection_model = self.loans_view.selectionModel()
        if not selection_model.hasSelection():
            return
        indices = selection_model.selectedRows()
        menu = QMenu(self)
        menu.addSection("Actions")
        view_action = menu.addAction(_("View in Libby"))
        view_action.triggered.connect(lambda: self.open_loan_in_libby(indices))
        return_action = menu.addAction(_("Return %d selected loan(s)") % len(indices))
        return_action.triggered.connect(lambda: self.return_selection(indices))
        menu.exec(QCursor.pos())

    def open_loan_in_libby(self, indices):
        for index in indices:
            loan = index.data(Qt.UserRole)
            library_key = next(
                iter(
                    [
                        c["advantageKey"]
                        for c in self.model._cards
                        if c["cardId"] == loan["cardId"]
                    ]
                ),
                "",
            )
            QDesktopServices.openUrl(
                QUrl(
                    f'https://libbyapp.com/library/{library_key}/everything/page-1/{loan["id"]}'
                )
            )

    def return_selection(self, indices):
        for index in reversed(indices):
            loan = index.data(Qt.UserRole)
            # logger.debug('Selected "%s" for return', loan["title"])
            self.return_loan(loan)
            self.model.removeRow(self.search_proxy_model.mapToSource(index).row())

    def return_loan(self, loan: Dict):
        description = _("Returning %s") % as_unicode(
            f'"{get_loan_title(loan)}"', errors="replace"
        )
        callback = Dispatcher(self.returned_loan)
        job = ThreadedJob(
            "overdrive_libby_return",
            description,
            guid_libby_return,
            (self.gui, self.client, loan),
            {},
            callback,
            max_concurrent_count=2,
            killable=False,
        )
        self.gui.job_manager.run_threaded_job(job)
        self.gui.status_bar.show_message(description, 3000)

    def returned_loan(self, job):
        if job.failed:
            self.gui.job_exception(job, dialog_title=_("Failed to return loan"))
            return

        self.gui.status_bar.show_message(job.description + " " + _("finished"), 5000)


class LibbyLoansModel(QAbstractTableModel):
    column_headers = [
        _("Title"),
        _("Author"),
        _("Checkout Date"),
        _("Type"),
        _("Format"),
    ]
    column_count = len(column_headers)
    filter_hide_books_already_in_library = False

    def __init__(self, parent, synced_state=None, db=None):
        super().__init__(parent)
        self.db = db
        self._cards = []
        self._loans = []
        self.filtered_loans = []
        self.filter_hide_books_already_in_library = PREFS[
            PreferenceKeys.HIDE_BOOKS_ALREADY_IN_LIB
        ]
        self.refresh_loans(synced_state)

    def refresh_loans(self, synced_state=None):
        if not synced_state:
            synced_state = {}
        self._cards = synced_state.get("cards", [])
        self._loans = sorted(
            synced_state.get("loans", []),
            key=lambda ln: ln["checkoutDate"],
            reverse=True,
        )
        self.filter_loans()

    def filter_loans(self):
        self.beginResetModel()
        self.filtered_loans = []
        for loan in [
            loan
            for loan in self._loans
            if (
                not PREFS[PreferenceKeys.HIDE_EBOOKS]
                and LibbyClient.is_downloadable_ebook_loan(loan)
            )
            or (
                not PREFS[PreferenceKeys.HIDE_MAGAZINES]
                and LibbyClient.is_downloadable_magazine_loan(loan)
            )
        ]:
            if not self.filter_hide_books_already_in_library:
                self.filtered_loans.append(loan)
                continue
            title = get_loan_title(loan)
            authors = []
            if loan.get("firstCreatorName", ""):
                authors = [loan.get("firstCreatorName", "")]
            if not self.db.has_book(Metadata(title=title, authors=authors)):
                self.filtered_loans.append(loan)
        self.endResetModel()

    def set_filter_hide_books_already_in_library(self, value: bool):
        if value != self.filter_hide_books_already_in_library:
            self.filter_hide_books_already_in_library = value
            self.filter_loans()

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if section >= len(self.column_headers):
            return None
        return self.column_headers[section]

    def rowCount(self, parent):
        return len(self.filtered_loans)

    def columnCount(self, parent):
        return self.column_count

    def data(self, index, role):
        row, col = index.row(), index.column()
        if row >= len(self.filtered_loans):
            return None
        loan = self.filtered_loans[row]
        if role == Qt.UserRole:
            return loan
        if role == Qt.TextAlignmentRole and col in (2, 3, 4):
            return Qt.AlignCenter
        if role != Qt.DisplayRole:
            return None
        if col >= self.column_count:
            return None
        if col == 0:
            return get_loan_title(loan)
        if col == 1:
            return loan.get("firstCreatorName", "")
        if col == 2:
            return parse_datetime(loan["checkoutDate"]).strftime("%Y-%m-%d")
        if col == 3:
            return loan.get("type", {}).get("id", "")
        if col == 4:
            return str(
                LibbyClient.get_loan_format(
                    loan, PREFS[PreferenceKeys.PREFER_OPEN_FORMATS]
                )
            )
        return None

    def removeRows(self, row, count, _):
        self.beginRemoveRows(QModelIndex(), row, row + count - 1)
        self.filtered_loans = (
            self.filtered_loans[:row] + self.filtered_loans[row + count :]
        )
        self.endRemoveRows()
        return True
