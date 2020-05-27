from typing import List, Dict

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.ui.widget import BaseCell, DirectionCell, EnumCell, BaseMonitor


from vnpy.app.option_master.base import ChainData
from vnpy.app.option_master.ui.monitor import MonitorCell, PosCell
from vnpy.app.option_master.ui.manager import AlgoSpinBox

from ..engine_ext import (
    APP_NAME, EVENT_OPTION_HEDGE_ALGO_STATUS,
    OptionEngineExt, HedgeEngine
)


class HedgeChainCombo(QtWidgets.QComboBox):
    def __init__(self, chain_symbol: str, monitor: "HedgeMonitor"):
        super().__init__()
        self.chain_symbol = chain_symbol
        self.monitor = monitor

    def get_value(self) -> str:
        return self.currentText()


class HedgeAutoButton(QtWidgets.QPushButton):
    def __init__(self, chain_symbol: str, monitor: "HedgeMonitor"):
        super().__init__()
        self.chain_symbol = chain_symbol
        self.monitor = monitor

        self.active = False
        self.setText("OFF")
        self.clicked.connect(self.on_clicked)

    def on_clicked(self) -> None:
        if self.active:
            self.monitor.stop_auto_hedge(self.chain_symbol)
        else:
            self.monitor.start_auto_hedge(self.chain_symbol)

    def update_status(self, active: bool) -> None:
        self.active = active

        if active:
            self.setText("ON")
        else:
            self.setText("OFF")


class HedgeActionButton(QtWidgets.QPushButton):
    def __init__(self, chain_symbol: str, monitor: "HedgeMonitor"):
        super().__init__()
        self.chain_symbol = chain_symbol
        self.monitor = monitor


class HedgePercentSpinBox(AlgoSpinBox):
    def __init__(self):
        super().__init__()
        self.setMaximum(100)
        self.setMinimum(10)
        self.setSingleStep(10)

    def get_value(self) -> float:
        return self.value() / 100


class OptionManagerExt(QtWidgets.QWidget):

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        """"""
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine
        self.option_engine = main_engine.get_engine(APP_NAME)

        self.hedge_manager: HedgeMonitor = None
        self.volatility_trading = None

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("OptionMasterExt")

        self.volatility_button = QtWidgets.QPushButton("波动率交易")
        self.hedge_button = QtWidgets.QPushButton("Delta对冲")

        for button in [
            self.volatility_button,
            self.hedge_button,
        ]:
            button.setEnabled(False)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.volatility_button)
        hbox.addWidget(self.hedge_button)

        self.setLayout(hbox)

    def register_event(self) -> None:
        pass



class StrategyOrderMonitor(BaseMonitor):
    event_type = EVENT_OPTION_STRATEGY_ORDER
    data_key = "strategy_order_id"
    sorting = True

    headers = {
        "strategy_order_id": {"display": "策略ID", "cell": BaseCell, "update": False},
        "chain_symbol": {"display": "期权链", "cell": BaseCell, "update": False},
        "time": {"display": "时间", "cell": BaseCell, "update": False},
        "strategy_name": {"display": "策略名", "cell": BaseCell, "update": False},
        "direction": {"display": "方向", "cell": BaseCell, "update": False},
        "status": {"display": "状态", "cell": EnumCell, "update": True},
    }

    def init_ui(self):
        super().init_ui()
        self.resize_columns()


class HedgeMonitor(QtWidgets.QTableWidget):
    signal_status = QtCore.pyqtSignal(Event)

    headers: List[Dict] = [
        {"name": "chain_symbol", "display": "期权链", "cell": MonitorCell},
        {"name": "status", "display": "状态", "cell": MonitorCell},
        {"name": "balance", "display": "中性基准价", "cell": MonitorCell},
        {"name": "up_threshold", "display": "上阈值", "cell": MonitorCell},
        {"name": "down_threshold", "display": "下阈值", "cell": MonitorCell},
        {"name": "pos_delta", "display": "Delta偏移量", "cell": MonitorCell},
        {"name": "net_pos", "display": "组合净仓", "cell": PosCell},
        {"name": "offset_percent", "display": "偏移比例", "cell": HedgePercentSpinBox},
        {"name": "hedge_percent", "display": "对冲比例", "cell": HedgePercentSpinBox},
        {"name": "auto_hedge", "display": "开始监测", "cell": HedgeAutoButton},
        {"name": "action_hedge", "display": "立即对冲", "cell": HedgeActionButton},
    ]

    def __init__(self, option_engine: OptionEngineExt):
        super().__init__()

        self.option_engine: OptionEngineExt = option_engine
        self.event_engine: EventEngine = option_engine.event_engine
        self.hedge_engine: HedgeEngine = self.option_engine.hedge_engine
        
        self.chains: Dict[str, ChainData] = self.hedge_engine.chains
        self.cells: Dict[str, Dict] = {}

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        self.setWindowTitle("通道对冲")
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.NoEditTriggers)

        chain_symbols = self.chains.keys()
        self.setRowCount(len(chain_symbols))
        self.setColumnCount(len(self.headers))

        labels = [d["display"] for d in self.headers]
        self.setHorizontalHeaderLabels(labels)

        for row, chain_symbol in enumerate(chain_symbols):
            portfolio_cells = {}
            for column, d in enumerate(self.headers):
                cell_type  = d['cell']
                cell_name = d['name']

                if cell_name in ['auto_hedge', 'action_hedge']:
                    cell = cell_type(chain_symbol, self)
                else:
                    cell = cell_type()

                if isinstance(cell, QtWidgets.QTableWidgetItem):
                    self.setItem(row, column, cell)
                else:
                    self.setCellWidget(row, column, cell)

                portfolio_cells[cell_name] = cell
            
            self.cells[chain_symbol] = portfolio_cells

        self.resizeColumnsToContents()

        for chain_symbol in self.cells:
            self.update_balance_price(chain_symbol)
            self.update_portfolio_attr(chain_symbol, 'net_pos')
            self.update_portfolio_attr(chain_symbol, 'pos_delta')

    def register_event(self) -> None:
        self.signal_status.connect(self.process_status_event)
        self.event_engine.register(EVENT_OPTION_HEDGE_ALGO_STATUS, self.signal_status.emit)

    def process_status_event(self, event: Event) -> None:
        algo = event.data
        cells = self.cells[algo.chain_symbol]

        cells['status'].setText(algo.status.value)

    def update_balance_price(self, chain_symbol: str):
        price = self.hedge_engine.get_balance_price(chain_symbol)
        cells = self.cells[chain_symbol]
        cells['balance_price'].setText(str(price))

    def update_portfolio_attr(self, chain_symbol: str, attr_name: str):
        chain = self.chains.get(chain_symbol)
        cells = self.cells[chain_symbol]

        if attr_name in cells:
            value = getattr(chain, attr_name, None)
            if value:
                cells['net_pos'].setText(str(value))
    
    def start_auto_hedge(self, chain_symbol) -> None:
        cells = self.cells[chain_symbol]
        params = {}
        for name in ['chain_symobl', 'offset_percent', 'hedge_percent']:
            params[name] = cells[name].get_value()

        self.hedge_engine.start_auto_hedge(chain_symbol, params)

    def stop_auto_hedge(self, chain_symbol) -> None:
        self.hedge_engine.stop_auto_hedge(chain_symbol)


class HedgeManager(QtWidgets.QWidget):

    signal_log = QtCore.pyqtSignal(Event)

    def __init__(self, option_engine: OptionEngineExt):
        self.option_engine = option_engine
        self.main_engine = option_engine.main_engine
        self.event_engine = option_engine.event_engine
        self.hedge_engine = option_engine.hedge_engine

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        self.setWindowTitle("Delta对冲")

        self.hedge_monitor = HedgeMonitor(self.option_engine)
        self.strategy_order_monitor = StrategyOrderMonitor(self.main_engine, self.event_engine)

        self.log_monitor = QtWidgets.QTextEdit()
        self.log_monitor.setReadOnly(True)
        self.log_monitor.setMaximumWidth(400)

        start_hedge_button = QtWidgets.QPushButton("启动自动对冲")
        start_hedge_button.clicked.connect(self.start_for_all)

        stop_hedge_button = QtWidgets.QPushButton("停止自动对冲")
        stop_hedge_button.clicked.connect(self.stop_for_all)

        self.offset_percent = HedgePercentSpinBox()
        self.hedge_percent = HedgePercentSpinBox()

        offset_percent_btn = QtWidgets.QPushButton("设置")
        offset_percent_btn.clicked.connect(self.set_offset_percent)

        hedge_percent_btn = QtWidgets.QPushButton("设置")
        hedge_percent_btn.clicked.connect(self.set_hedge_percent)

        QLabel = QtWidgets.QLabel
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QLabel("偏移比例"), 0, 0)
        grid.addWidget(self.offset_percent, 0, 1)
        grid.addWidget(offset_percent_btn, 0, 2)
        grid.addWidget(QLabel("对冲比例"), 1, 0)
        grid.addWidget(self.hedge_percent, 1, 1)
        grid.addWidget(hedge_percent_btn, 1, 2)

        left_vbox = QtWidgets.QVBoxLayout()
        left_vbox.addWidget(self.hedge_monitor)
        left_vbox.addWidget(self.strategy_order_monitor)

        ctrl_btn_hbox = QtWidgets.QHBoxLayout()
        ctrl_btn_hbox.addWidget(start_hedge_button)
        ctrl_btn_hbox.addWidget(stop_hedge_button)

        right_vbox = QtWidgets.QVBoxLayout()
        right_vbox.addLayout(ctrl_btn_hbox)
        right_vbox.addLayout(grid)
        right_vbox.addWidget(self.log_monitor)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addLayout(left_vbox)
        hbox.addLayout(right_vbox)

        self.setLayout(hbox)


    def register_event(self) -> None:
        """"""
        self.signal_log.connect(self.process_log_event)
        self.event_Engine.register(EVENT_OPTION_ALGO_LOG, self.signal_log.emit)

    def process_log_event(self, event: Event) -> None:
        """"""
        log = event.data
        timestr = log.time.strftime("%H:%M:%S")
        msg = f"{timestr}  {log.msg}"
        self.log_monitor.append(msg)

    def show(self) -> None:
        """"""
        self.hedge_engine.init_engine()
        self.algo_monitor.resizeColumnsToContents()
        super().showMaximized()

    def start_for_all(self) -> None:
        pass

    def stop_for_all(self) -> None:
        pass

    def set_offset_percent(self) -> None:
        pass

    def set_hedge_percent(self) -> None:
        pass