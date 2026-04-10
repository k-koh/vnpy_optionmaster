from datetime import datetime, timedelta
import pyqtgraph as pg
from typing import cast

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ, get_database, BaseDatabase
from vnpy.trader.object import BarData

from ..base import PortfolioData, OptionData, PreviousDayOptionData
from ..engine import OptionEngine, Event, EventEngine
from ..time import ANNUAL_DAYS

import numpy as np
import matplotlib
matplotlib.use('Qt5Agg')                    # noqa
import matplotlib.pyplot as plt             # noqa
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas  # noqa
from matplotlib.figure import Figure        # noqa
from mpl_toolkits.mplot3d import Axes3D     # noqa
from pylab import mpl                       # noqa

plt.style.use("dark_background")
mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei']   # set font for Chinese
mpl.rcParams['axes.unicode_minus'] = False


class OptionVolatilityChart(QtWidgets.QWidget):

    signal_timer: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        """"""
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.event_engine: EventEngine = option_engine.event_engine
        self.portfolio_name: str = portfolio_name

        self.timer_count: int = 0
        self.timer_trigger: int = 3

        self.chain_checks: dict[str, QtWidgets.QCheckBox] = {}
        self.put_bid_curves: dict[str, pg.PlotCurveItem] = {}
        self.put_ask_curves: dict[str, pg.PlotCurveItem] = {}
        self.put_mid_curves: dict[str, pg.PlotCurveItem] = {}

        self.call_bid_curves: dict[str, pg.PlotCurveItem] = {}
        self.call_ask_curves: dict[str, pg.PlotCurveItem] = {}
        self.call_mid_curves: dict[str, pg.PlotCurveItem] = {}
        # self.pricing_curves: dict[str, pg.PlotCurveItem] = {}
        self.eris_p_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.eris_c_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.delta002_c_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.atm_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.underlying_price_lines: dict[str, pg.InfiniteLine] = {}
        self.total_volume_bars: dict[str, pg.BarGraphItem] = {}

        self.prev_call_curves: dict[str, pg.PlotCurveItem] = {}
        self.prev_put_curves: dict[str, pg.PlotCurveItem] = {}

        self.iv_diff_pos_bars: dict[str, pg.BarGraphItem] = {}
        self.iv_diff_neg_bars: dict[str, pg.BarGraphItem] = {}
        self.iv_diff_pos_text_items: dict[str, list[pg.TextItem]] = {} # Added for IV diff text
        self.iv_diff_neg_text_items: dict[str, list[pg.TextItem]] = {} # Added for IV diff text
        self.total_volume_text_items: dict[str, list[pg.TextItem]] = {} # Added for Volume text
        self.chain_colors: dict[str, tuple] = {} # Added to store color for each chain

        self.underlying_line_positions: list[float] = [0.4, 0.3, 0.6, 0.2, 0.7, 0.1, 0.8, 0.9, 0.15]

        self.colors: list = [
            (255, 0, 0),
            (255, 255, 0),
            (0, 255, 0),
            (0, 0, 255),
            (0, 128, 0),
            (19, 234, 201),
            (195, 46, 212),
            (250, 194, 5),
            (0, 114, 189),
        ]

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("インプライド・ボラティリティ・カーブ（IV）")

        # Create checkbox for each chain
        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        chain_symbols: list = list(portfolio.chains.keys())
        chain_symbols.sort()

        hbox.addStretch()

        for chain_symbol in chain_symbols:
            chain_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
            chain_check.setText(chain_symbol.split(".")[0])
            chain_check.setChecked(True)
            chain_check.stateChanged.connect(self.update_curve_visible)

            hbox.addWidget(chain_check)
            self.chain_checks[chain_symbol] = chain_check

        hbox.addStretch()

        # Create graphics window
        pg.setConfigOptions(antialias=True)

        graphics_window: pg.GraphicsLayoutWidget = pg.GraphicsLayoutWidget()
        self.impv_chart = graphics_window.addPlot(row=0, col=0, title="インプライド・ボラティリティ・カーブ")
        self.impv_chart.showGrid(x=True, y=True)
        self.impv_chart.setLabel("left", "インプライド・ボラティリティ")
        self.impv_chart.setLabel("bottom", "権利行使価格")
        self.impv_chart.addLegend()
        self.impv_chart.setMenuEnabled(False)
        self.impv_chart.setMouseEnabled(False, False)

        graphics_window.nextRow()

        self.volume_chart = graphics_window.addPlot(row=1, col=0, title="出来高")
        self.volume_chart.showGrid(x=True, y=True)
        self.volume_chart.setLabel("left", "出来高")
        self.volume_chart.setLabel("bottom", "権利行使価格")
        self.volume_chart.setXLink(self.impv_chart)
        volume_legend = self.volume_chart.addLegend(colCount=2)
        volume_legend.anchor((0, 1), (0, 1)) # Anchor top-left of legend to top-left of plot
        volume_legend.setOffset((1, 1)) # Add padding (10 right, 10 down)
        self.volume_chart.setMenuEnabled(False)
        self.volume_chart.setMouseEnabled(False, False)
        self.volume_chart.setMaximumHeight(200)

        graphics_window.nextRow()

        self.iv_diff_chart = graphics_window.addPlot(row=2, col=0, title="前日比IV")
        self.iv_diff_chart.showGrid(x=True, y=True)
        self.iv_diff_chart.setLabel("left", "IV前日比")
        self.iv_diff_chart.setLabel("bottom", "権利行使価格")
        self.iv_diff_chart.setXLink(self.impv_chart)
        iv_diff_legend = self.iv_diff_chart.addLegend(colCount=4) # Changed colCount from 4 to 2 for consistency
        iv_diff_legend.anchor((0, 1), (0, 1)) # Anchor top-left of legend to top-left of plot
        iv_diff_legend.setOffset((1, 1)) # Add padding (10 right, 10 down)
        self.iv_diff_chart.setMenuEnabled(False)
        self.iv_diff_chart.setMouseEnabled(False, False)
        self.iv_diff_chart.setMaximumHeight(400)

        for chain_symbol in chain_symbols:
            self.add_impv_curve(chain_symbol)

        # Set Layout
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(graphics_window)
        self.setLayout(vbox)

    def register_event(self) -> None:
        """"""
        self.signal_timer.connect(self.process_timer_event)

        self.event_engine.register(EVENT_TIMER, self.signal_timer.emit)

    def process_timer_event(self, event: Event) -> None:
        """"""
        self.timer_count += 1
        if self.timer_count < self.timer_trigger:
            return
        self.timer_trigger = 0

        self.update_curve_data()
        self.update_curve_visible() # Ensure visibility is updated for newly created items

    def add_impv_curve(self, chain_symbol: str) -> None:
        """"""
        symbol_size: int = 14
        symbol: str = chain_symbol.split(".")[0]
        color: tuple = self.colors.pop(0)
        self.chain_colors[chain_symbol] = color # Store the color for this chain
        pen: QtGui.QPen = pg.mkPen(color, width=2)
        pen_dot: QtGui.QPen = pg.mkPen(color, style=QtCore.Qt.DotLine)
        pen_prev_day: QtGui.QPen = pg.mkPen(color, style=QtCore.Qt.DashLine)

        self.call_mid_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " コール",
            pen=pen,
        )
        self.put_mid_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " プット",
            pen=pen,
        )

        self.call_bid_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t1",
            name=symbol + " コール買",
            symbolBrush=color
        )
        self.call_ask_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t1",
            name=symbol + " コール売",
            symbolBrush=color
        )
        self.put_bid_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t",
            name=symbol + " プット買",
            symbolBrush=color
        )
        self.put_ask_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t",
            name=symbol + " プット売",
            symbolBrush=color
        )

        # self.pricing_curves[chain_symbol] = self.impv_chart.plot(
        #     symbolSize=symbol_size,
        #     symbol="o",
        #     name=symbol + " 定价",
        #     pen=pen_dot,
        #     symbolBrush=color
        # )

        self.prev_call_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 前日コール",
            pen=pen_prev_day,
        )
        self.prev_put_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 前日プット",
            pen=pen_prev_day,
        )

        p_line_pen = pg.mkPen(color=color, width=2, style=QtCore.Qt.DotLine)
        c_line_pen = pg.mkPen(color=color, width=2, style=QtCore.Qt.DotLine)
        atm_line_pen = pg.mkPen(color=(255, 174, 201), width=2, style=QtCore.Qt.DotLine)
        underlying_line_pen = pg.mkPen(color=(0, 255, 255), width=2, style=QtCore.Qt.DotLine)
        position = self.underlying_line_positions.pop(0)

        self.eris_p_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=p_line_pen,
            label=symbol + " プットΔ0.1",
            labelOpts={'position': 0.95, 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )
        self.eris_c_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=c_line_pen,
            label=symbol + " コールΔ0.1",
            labelOpts={'position': 0.01, 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )

        self.delta002_c_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=c_line_pen,
            label=symbol + " コールΔ0.022",
            labelOpts={'position': 0.12, 'color': color, 'fill': (200, 200, 200, 50), 'movable': False}
        )

        self.atm_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=atm_line_pen,
            label=symbol + " ATM",
            labelOpts={'position': 0.5, 'color': (255, 174, 201), 'fill': (200,200,200,50), 'movable': False}
        )
        self.underlying_price_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=underlying_line_pen,
            label=symbol + " 先物",
            labelOpts={'position': position, 'color': (0, 255, 255), 'fill': (200, 200, 200, 50), 'movable': False}
        )

        self.impv_chart.addItem(self.eris_p_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.eris_c_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.atm_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.underlying_price_lines[chain_symbol])
        self.impv_chart.addItem(self.delta002_c_strike_lines[chain_symbol])

        self.eris_p_strike_lines[chain_symbol].hide()
        self.eris_c_strike_lines[chain_symbol].hide()
        self.atm_strike_lines[chain_symbol].hide()
        self.underlying_price_lines[chain_symbol].hide()
        self.delta002_c_strike_lines[chain_symbol].hide()

        self.total_volume_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color), # Use a neutral color for combined volume
            name=symbol
        )
        self.volume_chart.addItem(self.total_volume_bars[chain_symbol])

        self.iv_diff_pos_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color),
            name=symbol
        )
        self.iv_diff_neg_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color + (100,)),
            name=symbol
        )
        self.iv_diff_chart.addItem(self.iv_diff_pos_bars[chain_symbol])
        self.iv_diff_chart.addItem(self.iv_diff_neg_bars[chain_symbol])

        self.iv_diff_pos_text_items[chain_symbol] = [] # Initialize list for text items
        self.iv_diff_neg_text_items[chain_symbol] = []
        self.total_volume_text_items[chain_symbol] = []

    def update_curve_data(self) -> None:
        """"""
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        prev_day_data: PreviousDayOptionData = self.option_engine.prev_day_option

        max_volumes: float = 0.0
        max_iv_diff_pos: float = 0.0
        min_iv_diff_neg: float = 0.0

        # First, calculate max values from selected chains for text label offsetting
        for chain in portfolio.chains.values():
            if not self.chain_checks[chain.chain_symbol].isChecked():
                continue

            calls: list[OptionData] = list(chain.calls.values())
            calls.sort(key=lambda x: x.strike_price)
            call_strikes = [c.strike_price for c in calls]
            call_volumes = [c.tick.volume if c.tick else 0 for c in calls]
            call_mid_impv = [c.mid_impv * 100 for c in calls]

            puts: list[OptionData] = list(chain.puts.values())
            puts.sort(key=lambda x: x.strike_price)
            put_strikes = [p.strike_price for p in puts]
            put_volumes = [p.tick.volume if p.tick else 0 for p in puts]
            put_mid_impv = [p.mid_impv * 100 for p in puts]

            all_strikes = sorted(list(set(call_strikes + put_strikes)))

            # Calculate total volumes
            call_volume_map = {s: v for s, v in zip(call_strikes, call_volumes)}
            put_volume_map = {s: v for s, v in zip(put_strikes, put_volumes)}
            total_volumes = [call_volume_map.get(s, 0) + put_volume_map.get(s, 0) for s in all_strikes]
            if total_volumes:
                max_volumes = max(max_volumes, max(total_volumes))

            # Calculate IV difference
            prev_call_ivs: list = []
            prev_put_ivs: list = []
            if prev_day_data:
                dt: datetime = datetime.now(DB_TZ)
                prev_call_data, prev_put_data = prev_day_data.get_prev_day_iv_curve(dt, chain)
                prev_call_ivs = [prev_call_data.get(s, 0) * 100 for s in call_strikes]
                prev_put_ivs = [prev_put_data.get(s, 0) * 100 for s in put_strikes]

            prev_call_iv_map = {s: v for s, v in zip(call_strikes, prev_call_ivs)}
            prev_put_iv_map = {s: v for s, v in zip(put_strikes, prev_put_ivs)}
            call_mid_impv_map = {s: v for s, v in zip(call_strikes, call_mid_impv)}
            put_mid_impv_map = {s: v for s, v in zip(put_strikes, put_mid_impv)}

            iv_diff_heights = []
            for strike in all_strikes:
                call_iv = call_mid_impv_map.get(strike, 0)
                put_iv = put_mid_impv_map.get(strike, 0)
                prev_call_iv = prev_call_iv_map.get(strike, 0)
                prev_put_iv = prev_put_iv_map.get(strike, 0)
                has_call = call_iv > 0
                has_put = put_iv > 0
                current_iv = 0
                prev_iv = 0

                if has_call and has_put:
                    current_iv = (call_iv + put_iv) / 2.0
                    p_count = 0
                    p_sum = 0
                    if prev_call_iv > 0:
                        p_sum += prev_call_iv
                        p_count += 1
                    if prev_put_iv > 0:
                        p_sum += prev_put_iv
                        p_count += 1
                    if p_count > 0:
                        prev_iv = p_sum / p_count
                elif has_call:
                    current_iv = call_iv
                    prev_iv = prev_call_iv
                elif has_put:
                    current_iv = put_iv
                    prev_iv = prev_put_iv
                else:
                    continue

                if prev_iv != 0:
                    iv_diff_heights.append(current_iv - prev_iv)

            pos_heights = [h for h in iv_diff_heights if h >= 0]
            neg_heights = [h for h in iv_diff_heights if h < 0]

            if pos_heights:
                max_iv_diff_pos = max(max_iv_diff_pos, max(pos_heights))
            if neg_heights:
                min_iv_diff_neg = min(min_iv_diff_neg, min(neg_heights))

        text_offset_scale: float = 6.0/15.0

        for chain in portfolio.chains.values():
            text_offset_scale = text_offset_scale - 1.0/15.0
            # Clear previous text items for IV diff chart
            for text_item in self.iv_diff_pos_text_items[chain.chain_symbol]:
                self.iv_diff_chart.removeItem(text_item)
            self.iv_diff_pos_text_items[chain.chain_symbol].clear()

            for text_item in self.iv_diff_neg_text_items[chain.chain_symbol]:
                self.iv_diff_chart.removeItem(text_item)
            self.iv_diff_neg_text_items[chain.chain_symbol].clear()

            # Clear previous text items for Volume chart
            for text_item in self.total_volume_text_items[chain.chain_symbol]:
                self.volume_chart.removeItem(text_item)
            self.total_volume_text_items[chain.chain_symbol].clear()

            # Get call data
            call_mid_impv: list = []
            call_bid_impv: list = []
            call_ask_impv: list = []
            # pricing_impv: list = []
            call_strikes: list = []
            call_volumes: list = []

            calls: list[OptionData] = list(chain.calls.values())
            calls.sort(key=lambda x: x.strike_price)

            for call in calls:
                mid_impv = call.mid_impv * 100
                bid_impv = call.bid_impv * 100
                ask_impv = call.ask_impv * 100

                call_mid_impv.append(mid_impv if mid_impv else np.nan)
                call_bid_impv.append(bid_impv if bid_impv else np.nan)
                call_ask_impv.append(ask_impv if ask_impv else np.nan)

                # pricing_impv.append(call.pricing_impv * 100)
                call_strikes.append(call.strike_price)

                if call.tick:
                    call_volumes.append(call.tick.volume)
                else:
                    call_volumes.append(0)

            # Get put data
            put_mid_impv: list = []
            put_bid_impv: list = []
            put_ask_impv: list = []
            put_strikes: list = []
            put_volumes: list = []

            puts: list[OptionData] = list(chain.puts.values())
            puts.sort(key=lambda x: x.strike_price)

            for put in puts:
                mid_impv = put.mid_impv * 100
                bid_impv = put.bid_impv * 100
                ask_impv = put.ask_impv * 100

                put_mid_impv.append(mid_impv if mid_impv else np.nan)
                put_bid_impv.append(bid_impv if bid_impv else np.nan)
                put_ask_impv.append(ask_impv if ask_impv else np.nan)
                put_strikes.append(put.strike_price)

                if put.tick:
                    put_volumes.append(put.tick.volume)
                else:
                    put_volumes.append(0)

            # Get previous day iv
            prev_call_ivs: list = []
            prev_put_ivs: list = []
            if prev_day_data:
                dt: datetime = datetime.now(DB_TZ)

                prev_call_data, prev_put_data = prev_day_data.get_prev_day_iv_curve(dt, chain)

                for strike in call_strikes:
                    iv = prev_call_data.get(strike, 0)
                    prev_call_ivs.append(iv * 100 if iv else np.nan)

                for strike in put_strikes:
                    iv = prev_put_data.get(strike, 0)
                    prev_put_ivs.append(iv * 100 if iv else np.nan)

            # Calculate IV difference
            iv_diff_strikes = []
            iv_diff_heights = []

            all_strikes = sorted(list(set(call_strikes + put_strikes)))
            prev_call_iv_map = {s: v for s, v in zip(call_strikes, prev_call_ivs)}
            prev_put_iv_map = {s: v for s, v in zip(put_strikes, prev_put_ivs)}
            call_mid_impv_map = {s: v for s, v in zip(call_strikes, call_mid_impv)}
            put_mid_impv_map = {s: v for s, v in zip(put_strikes, put_mid_impv)}

            for strike in all_strikes:
                call_iv = call_mid_impv_map.get(strike, 0)
                put_iv = put_mid_impv_map.get(strike, 0)
                prev_call_iv = prev_call_iv_map.get(strike, 0)
                prev_put_iv = prev_put_iv_map.get(strike, 0)

                has_call = call_iv > 0
                has_put = put_iv > 0

                current_iv = 0
                prev_iv = 0

                if has_call and has_put:
                    current_iv = (call_iv + put_iv) / 2.0

                    p_count = 0
                    p_sum = 0
                    if prev_call_iv > 0:
                        p_sum += prev_call_iv
                        p_count += 1
                    if prev_put_iv > 0:
                        p_sum += prev_put_iv
                        p_count += 1
                    if p_count > 0:
                        prev_iv = p_sum / p_count

                elif has_call:
                    current_iv = call_iv
                    prev_iv = prev_call_iv
                elif has_put:
                    current_iv = put_iv
                    prev_iv = prev_put_iv
                else:
                    continue

                if prev_iv != 0:
                    iv_diff_strikes.append(strike)
                    iv_diff_heights.append(current_iv - prev_iv)

            iv_diff_data = list(zip(iv_diff_strikes, iv_diff_heights))
            iv_diff_data.sort(key=lambda item: abs(item[1]), reverse=True)

            pos_strikes = []
            pos_heights = []
            neg_strikes = []
            neg_heights = []

            for strike, height in iv_diff_data:
                if height >= 0:
                    pos_strikes.append(strike)
                    pos_heights.append(height)
                else:
                    neg_strikes.append(strike)
                    neg_heights.append(height)

            # Plot curves
            def set_curve_data(curve, x_data, y_data):
                if not y_data or np.all(np.isnan(np.array(y_data, dtype=float))):
                    curve.setData(x=[], y=[])
                else:
                    curve.setData(x=x_data, y=y_data)

            set_curve_data(self.call_mid_curves[chain.chain_symbol], call_strikes, call_mid_impv)
            set_curve_data(self.call_bid_curves[chain.chain_symbol], call_strikes, call_bid_impv)
            set_curve_data(self.call_ask_curves[chain.chain_symbol], call_strikes, call_ask_impv)
            set_curve_data(self.put_mid_curves[chain.chain_symbol], put_strikes, put_mid_impv)
            set_curve_data(self.put_bid_curves[chain.chain_symbol], put_strikes, put_bid_impv)
            set_curve_data(self.put_ask_curves[chain.chain_symbol], put_strikes, put_ask_impv)

            # self.pricing_curves[chain.chain_symbol].setData(
            #     y=pricing_impv,
            #     x=call_strikes
            # )

            if prev_call_ivs and prev_put_ivs:
                set_curve_data(self.prev_call_curves[chain.chain_symbol], call_strikes, prev_call_ivs)
                set_curve_data(self.prev_put_curves[chain.chain_symbol], put_strikes, prev_put_ivs)

            # Update ERIS strike lines
            symbol = chain.chain_symbol.split(".")[0]
            if chain.eris_p_strike is not None:
                line = self.eris_p_strike_lines[chain.chain_symbol]
                line.setPos(chain.eris_p_strike)
                delta_text = f"{chain.eris_p_delta:.3f}" if chain.eris_p_delta is not None else "N/A"
                iv_text = f"{chain.eris_p_iv:.3f}" if chain.eris_p_iv is not None else "N/A"
                price_text = f"{chain.eris_p_price:.0f}" if chain.eris_p_price is not None else "N/A"
                line.label.setText(f"{symbol} PΔ{delta_text} IV{iv_text} ¥{price_text}")
                line.show()
            else:
                self.eris_p_strike_lines[chain.chain_symbol].hide()

            if chain.eris_c_strike is not None:
                line = self.eris_c_strike_lines[chain.chain_symbol]
                line.setPos(chain.eris_c_strike)
                delta_text = f"{chain.eris_c_delta:.3f}" if chain.eris_c_delta is not None else "N/A"
                iv_text = f"{chain.eris_c_iv:.3f}" if chain.eris_c_iv is not None else "N/A"
                price_text = f"{chain.eris_c_price:.0f}" if chain.eris_c_price is not None else "N/A"
                line.label.setText(f"{symbol} CΔ{delta_text} IV{iv_text} ¥{price_text}")
                line.show()
            else:
                self.eris_c_strike_lines[chain.chain_symbol].hide()

            # Update Delta strike lines
            if hasattr(chain, "delta002_c_strike") and chain.delta002_c_strike is not None:
                line = self.delta002_c_strike_lines[chain.chain_symbol]
                line.setPos(chain.delta002_c_strike)
                delta_text = f"{chain.delta002_c_delta:.4f}" if getattr(chain, "delta002_c_delta", None) is not None else "N/A"
                line.label.setText(f"{symbol} CΔ{delta_text}")
                line.show()
            else:
                self.delta002_c_strike_lines[chain.chain_symbol].hide()

            # Update ATM strike line
            if chain.atm_price:
                self.atm_strike_lines[chain.chain_symbol].setPos(chain.atm_price)
                self.atm_strike_lines[chain.chain_symbol].show()
            else:
                self.atm_strike_lines[chain.chain_symbol].hide()

            # Update underlying price line
            underlying_price = None
            if calls:
                underlying_price = calls[0].underlying.mid_price
            elif puts:
                underlying_price = puts[0].underlying.mid_price

            if underlying_price:
                line = self.underlying_price_lines[chain.chain_symbol]
                line.setPos(underlying_price)
                symbol = chain.chain_symbol.split(".")[0]
                line.label.setText(f"{symbol} 先物: {underlying_price:.0f}")
                line.show()
            else:
                self.underlying_price_lines[chain.chain_symbol].hide()

            # Calculate strike_step
            strike_step = 0
            if len(all_strikes) > 1:
                all_strikes.sort() # Ensure sorted to correctly calculate step
                strike_step = all_strikes[1] - all_strikes[0]

            # Update volume bars
            volume_strikes = sorted(list(set(call_strikes + put_strikes)))
            call_volume_map = {s: v for s, v in zip(call_strikes, call_volumes)}
            put_volume_map = {s: v for s, v in zip(put_strikes, put_volumes)}

            total_volumes = []
            for s in volume_strikes:
                total_vol = call_volume_map.get(s, 0) + put_volume_map.get(s, 0)
                total_volumes.append(total_vol)

            volume_data = list(zip(volume_strikes, total_volumes))
            volume_data.sort(key=lambda item: item[1], reverse=True)
            sorted_volume_strikes = [item[0] for item in volume_data]
            sorted_total_volumes = [item[1] for item in volume_data]

            bar_width = strike_step * 0.25 if strike_step else 100

            self.total_volume_bars[chain.chain_symbol].setOpts(
                x=sorted_volume_strikes, height=sorted_total_volumes, width=bar_width
            )

            # Add text labels for Volume bars
            chain_color = self.chain_colors[chain.chain_symbol]
            font = QtGui.QFont()
            font.setPointSize(8)

            # Calculate dynamic offset based on the maximum volume
            if max_volumes > 0:
                volume_text_offset = max_volumes * text_offset_scale
            else:
                volume_text_offset = 20 # Default offset

            for strike, volume in zip(sorted_volume_strikes, sorted_total_volumes):
                if volume == 0:
                    continue
                text_item = pg.TextItem(
                    text=f"{volume:.0f}",
                    color=chain_color,
                    anchor=(0.5, 0)
                )
                text_item.setFont(font)
                text_item.setPos(strike, volume + volume_text_offset) # Position with dynamic offset
                self.volume_chart.addItem(text_item)
                self.total_volume_text_items[chain.chain_symbol].append(text_item)

            bar_width_diff = bar_width
            self.iv_diff_pos_bars[chain.chain_symbol].setOpts(x=pos_strikes, height=pos_heights, width=bar_width_diff)
            self.iv_diff_neg_bars[chain.chain_symbol].setOpts(x=neg_strikes, height=neg_heights, width=bar_width_diff)

            # Add text labels for IV diff bars
            chain_color = self.chain_colors[chain.chain_symbol]
            font = QtGui.QFont()
            font.setPointSize(8) # Smaller font size for better fit

            if max_iv_diff_pos > 0:
                iv_text_offset_pos = max_iv_diff_pos * text_offset_scale
            else:
                iv_text_offset_pos = 0.5

            for strike, height in zip(pos_strikes, pos_heights):
                text_item = pg.TextItem(
                    text=f"{height:.2f}%",
                    color=chain_color,
                    anchor=(0.5, 0) # Center above the bar
                )
                text_item.setFont(font)
                text_item.setPos(strike, height + iv_text_offset_pos)
                self.iv_diff_chart.addItem(text_item)
                self.iv_diff_pos_text_items[chain.chain_symbol].append(text_item)

            if min_iv_diff_neg < 0:
                iv_text_offset_neg = abs(min_iv_diff_neg) * text_offset_scale
            else:
                iv_text_offset_neg = 0.5

            for strike, height in zip(neg_strikes, neg_heights):
                text_item = pg.TextItem(
                    text=f"{height:.2f}%",
                    color=chain_color,
                    anchor=(0.5, 1) # Center below the bar
                )
                text_item.setFont(font)
                text_item.setPos(strike, height - iv_text_offset_neg)
                self.iv_diff_chart.addItem(text_item)
                self.iv_diff_neg_text_items[chain.chain_symbol].append(text_item)

        # Set Y-range for volume chart to provide padding for text labels
        y_max_volume = max_volumes * 1.5 if max_volumes > 0 else 10
        self.volume_chart.setYRange(0, y_max_volume)

        # Set Y-range for IV diff chart to provide padding for text labels
        padding_pos = max_iv_diff_pos * 0.5 if max_iv_diff_pos > 0 else 0.5
        padding_neg = abs(min_iv_diff_neg * 0.5) if min_iv_diff_neg < 0 else 0.5

        y_max_iv = max_iv_diff_pos + padding_pos
        y_min_iv = min_iv_diff_neg - padding_neg

        if y_min_iv >= y_max_iv:
            y_min_iv = -5
            y_max_iv = 5

        self.iv_diff_chart.setYRange(y_min_iv, y_max_iv)

    def update_curve_visible(self) -> None:
        """"""
        for chain_symbol, checkbox in self.chain_checks.items():
            call_mid_curve: pg.PlotCurveItem = self.call_mid_curves[chain_symbol]
            call_bid_curve: pg.PlotCurveItem = self.call_bid_curves[chain_symbol]
            call_ask_curve: pg.PlotCurveItem = self.call_ask_curves[chain_symbol]
            put_mid_curve: pg.PlotCurveItem = self.put_mid_curves[chain_symbol]
            put_bid_curve: pg.PlotCurveItem = self.put_bid_curves[chain_symbol]
            put_ask_curve: pg.PlotCurveItem = self.put_ask_curves[chain_symbol]
            # pricing_curve: pg.PlotCurveItem = self.pricing_curves[chain_symbol]
            prev_call_curve: pg.PlotCurveItem = self.prev_call_curves[chain_symbol]
            prev_put_curve: pg.PlotCurveItem = self.prev_put_curves[chain_symbol]
            p_line = self.eris_p_strike_lines[chain_symbol]
            c_line = self.eris_c_strike_lines[chain_symbol]
            delta_f_c_line = self.delta002_c_strike_lines[chain_symbol]
            atm_line = self.atm_strike_lines[chain_symbol]
            underlying_line = self.underlying_price_lines[chain_symbol]
            total_volume_bar = self.total_volume_bars[chain_symbol]
            iv_diff_pos_bar = self.iv_diff_pos_bars[chain_symbol]
            iv_diff_neg_bar = self.iv_diff_neg_bars[chain_symbol]

            if checkbox.isChecked():
                call_mid_curve.show()
                call_bid_curve.show()
                call_ask_curve.show()
                put_mid_curve.show()
                put_bid_curve.show()
                put_ask_curve.show()
                # pricing_curve.show()
                prev_call_curve.show()
                prev_put_curve.show()
                total_volume_bar.show()
                iv_diff_pos_bar.show()
                iv_diff_neg_bar.show()
                for text_item in self.iv_diff_pos_text_items[chain_symbol]:
                    text_item.show()
                for text_item in self.iv_diff_neg_text_items[chain_symbol]:
                    text_item.show()
                for text_item in self.total_volume_text_items[chain_symbol]:
                    text_item.show()
            else:
                call_mid_curve.hide()
                call_bid_curve.hide()
                call_ask_curve.hide()
                put_mid_curve.hide()
                put_bid_curve.hide()
                put_ask_curve.hide()
                # pricing_curve.hide()
                prev_call_curve.hide()
                prev_put_curve.hide()
                p_line.hide()
                c_line.hide()
                delta_f_c_line.hide()
                atm_line.hide()
                underlying_line.hide()
                total_volume_bar.hide()
                iv_diff_pos_bar.hide()
                iv_diff_neg_bar.hide()
                for text_item in self.iv_diff_pos_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.iv_diff_neg_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.total_volume_text_items[chain_symbol]:
                    text_item.hide()


class ScenarioAnalysisChart(QtWidgets.QWidget):
    """"""

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        """"""
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("情景分析")

        # Create widgets
        self.price_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.price_change_spin.setSuffix("%")
        self.price_change_spin.setMinimum(2)
        self.price_change_spin.setValue(10)

        self.impv_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.impv_change_spin.setSuffix("%")
        self.impv_change_spin.setMinimum(2)
        self.impv_change_spin.setValue(10)

        self.time_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.time_change_spin.setSuffix("日")
        self.time_change_spin.setMinimum(0)
        self.time_change_spin.setValue(1)

        self.target_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.target_combo.addItems([
            "盈亏",
            "Delta",
            "Gamma",
            "Theta",
            "Vega"
        ])

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("执行分析")
        button.clicked.connect(self.run_analysis)

        # Create charts
        fig: Figure = Figure()
        canvas: FigureCanvas = FigureCanvas(fig)

        ax = fig.add_subplot(projection="3d")
        self.ax = cast(Axes3D, ax)
        self.ax.set_xlabel("价格涨跌 %")
        self.ax.set_ylabel("波动率涨跌 %")
        self.ax.set_zlabel("盈亏")

        # Set layout
        hbox1: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox1.addWidget(QtWidgets.QLabel("目标数据"))
        hbox1.addWidget(self.target_combo)
        hbox1.addWidget(QtWidgets.QLabel("时间衰减"))
        hbox1.addWidget(self.time_change_spin)
        hbox1.addStretch()

        hbox2: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox2.addWidget(QtWidgets.QLabel("价格变动"))
        hbox2.addWidget(self.price_change_spin)
        hbox2.addWidget(QtWidgets.QLabel("波动率变动"))
        hbox2.addWidget(self.impv_change_spin)
        hbox2.addStretch()
        hbox2.addWidget(button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)
        vbox.addWidget(canvas)

        self.setLayout(vbox)

    def run_analysis(self) -> None:
        """"""
        # Generate range
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        price_change_range = self.price_change_spin.value()
        price_changes = np.arange(-price_change_range, price_change_range + 1) / 100

        impv_change_range = self.impv_change_spin.value()
        impv_changes = np.arange(-impv_change_range, impv_change_range + 1) / 100

        time_change = self.time_change_spin.value() / ANNUAL_DAYS
        target_name = self.target_combo.currentText()

        # Check underlying price exists
        for underlying in portfolio.underlyings.values():
            if not underlying.mid_price:
                QtWidgets.QMessageBox.warning(
                    self,
                    "无法执行情景分析",
                    f"标的物{underlying.symbol}当前中间价为{underlying.mid_price}",
                    QtWidgets.QMessageBox.Ok
                )
                return

        # Run analysis calculation
        pnls: list = []
        deltas: list = []
        gammas: list = []
        thetas: list = []
        vegas: list = []

        for impv_change in impv_changes:
            pnl_buf: list = []
            delta_buf: list = []
            gamma_buf: list = []
            theta_buf: list = []
            vega_buf: list = []

            for price_change in price_changes:
                portfolio_pnl = 0
                portfolio_delta = 0.0
                portfolio_gamma = 0
                portfolio_theta = 0
                portfolio_vega = 0

                # Calculate underlying pnl
                for underlying in portfolio.underlyings.values():
                    if not underlying.net_pos:
                        continue

                    value = underlying.mid_price * underlying.net_pos * underlying.size
                    portfolio_pnl += value * price_change
                    portfolio_delta += value / 100

                # Calculate option pnl
                for option in portfolio.options.values():
                    if not option.net_pos:
                        continue

                    new_underlying_price = option.underlying.mid_price * (1 + price_change)
                    new_time_to_expiry = max(option.time_to_expiry - time_change, 0)
                    new_mid_impv = option.mid_impv * (1 + impv_change)

                    new_price, delta, gamma, theta, vega = option.calculate_greeks(
                        new_underlying_price,
                        option.strike_price,
                        option.interest_rate,
                        new_time_to_expiry,
                        new_mid_impv,
                        option.option_type
                    )

                    # 添加对option.tick为None的检查
                    if option.tick is None:
                        diff = 0
                    else:
                        diff = new_price - option.tick.last_price
                    multiplier = option.net_pos * option.size

                    portfolio_pnl += diff * multiplier
                    portfolio_delta += delta * multiplier
                    portfolio_gamma += gamma * multiplier
                    portfolio_theta += theta * multiplier
                    portfolio_vega += vega * multiplier

                pnl_buf.append(portfolio_pnl)
                delta_buf.append(portfolio_delta)
                gamma_buf.append(gamma_buf)
                theta_buf.append(theta_buf)
                vega_buf.append(vega_buf)

            pnls.append(pnl_buf)
            deltas.append(delta_buf)
            gammas.append(gamma_buf)
            thetas.append(theta_buf)
            vegas.append(vega_buf)

        # Plot chart
        if target_name == "盈亏":
            target_data: list = pnls
        elif target_name == "Delta":
            target_data = deltas
        elif target_name == "Gamma":
            target_data = gammas
        elif target_name == "Theta":
            target_data = thetas
        else:
            target_data = vegas

        self.update_chart(price_changes * 100, impv_changes * 100, target_data, target_name)

    def update_chart(
        self,
        price_changes: np.ndarray,
        impv_changes: np.ndarray,
        target_data: list[list[float]],
        target_name: str
    ) -> None:
        """"""
        self.ax.clear()

        price_changes, impv_changes = np.meshgrid(price_changes, impv_changes)

        self.ax.set_zlabel(target_name)
        self.ax.plot_surface(
            X=price_changes,
            Y=impv_changes,
            Z=np.array(target_data),
            rstride=1,
            cstride=1,
            cmap='coolwarm'
        )


def _load_option_bars_with_today(days: int) -> list[BarData]:
    """DAILYバー + 当日MINUTEフォールバックでオプションバーを取得する共通関数

    セッション構成:
      ナイトセッション 17:00～翌06:00 + デイセッション 08:45～15:40
      → 翌日15:45のdatetimeでDAILYバーとして保存される
    例: 4/7 17:00 ～ 4/8 15:40 のデータ → datetime=4/8 15:45 として保存
    """
    now: datetime = datetime.now(DB_TZ)
    start: datetime = now - timedelta(days=days)

    # 17:00以降はナイトセッション開始 = 翌営業日のデータ
    # DAILYバーは翌日15:45で保存されるため、endを翌日末まで拡張
    if now.hour >= 17:
        session_date: datetime = (now + timedelta(days=1))
    else:
        session_date = now
    end: datetime = session_date.replace(hour=23, minute=59, second=59, microsecond=0)

    database: BaseDatabase = get_database()
    daily_bars: list[BarData] = database.load_option_data(
        symbol="",
        exchange=Exchange.JPX,
        interval=Interval.DAILY,
        start=start,
        end=end,
    )

    # 現在のセッション日のDAILYバーがあるか確認
    session_date_str: str = session_date.strftime("%Y-%m-%d")
    has_session_data: bool = any(
        bar.datetime.strftime("%Y-%m-%d") == session_date_str for bar in daily_bars
    )

    if not has_session_data:
        # ナイトセッション開始（当日or前日17:00）からのMINUTEバーを取得
        if now.hour >= 17:
            minute_start: datetime = now.replace(
                hour=17, minute=0, second=0, microsecond=0
            )
        else:
            minute_start = (now - timedelta(days=1)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
        minute_bars: list[BarData] = database.load_option_data(
            symbol="",
            exchange=Exchange.JPX,
            interval=Interval.MINUTE,
            start=minute_start,
            end=now,
        )

        if minute_bars:
            latest_by_symbol: dict[str, BarData] = {}
            for bar in minute_bars:
                existing = latest_by_symbol.get(bar.symbol)
                if existing is None or bar.datetime > existing.datetime:
                    latest_by_symbol[bar.symbol] = bar

            # セッション日の15:45として統一
            session_dt: datetime = session_date.replace(
                hour=15, minute=45, second=0, microsecond=0
            )
            for bar in latest_by_symbol.values():
                bar.datetime = session_dt
                bar.interval = Interval.DAILY
                daily_bars.append(bar)

    return daily_bars


def _extract_month(bar: BarData) -> str:
    """シンボル(例: nk-2604-C-35000)から限月部分を抽出"""
    parts: list[str] = bar.symbol.split("-")
    return parts[1] if len(parts) >= 2 else ""


def _filter_bars_by_month(bars: list[BarData], month: str) -> list[BarData]:
    """限月でバーをフィルタ。'全て'の場合はフィルタなし"""
    if month == "全て":
        return bars
    return [b for b in bars if _extract_month(b) == month]


def _populate_month_combo(combo: QtWidgets.QComboBox, bars: list[BarData]) -> None:
    """バーから限月一覧を取得してコンボボックスに設定"""
    prev_text: str = combo.currentText()
    months: set[str] = set()
    for bar in bars:
        m: str = _extract_month(bar)
        if m:
            months.add(m)
    combo.clear()
    combo.addItem("全て")
    for m in sorted(months):
        combo.addItem(m)
    # 以前の選択を復元
    idx: int = combo.findText(prev_text)
    if idx >= 0:
        combo.setCurrentIndex(idx)


class IVHeatmapChart(QtWidgets.QWidget):
    """IV残像ヒートマップ - デルタレベル別IV推移の可視化"""

    # Target delta values: negative = put, positive = call
    DELTA_TARGETS: list[tuple[str, float]] = [
        ("Put Δ0.02", -0.02),
        ("Put Δ0.10", -0.10),
        ("Put Δ0.20", -0.20),
        ("ATM (Δ0.50)", -0.50),
        ("Call Δ0.20", 0.20),
        ("Call Δ0.10", 0.10),
        ("Call Δ0.02", 0.02),
    ]

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name
        self.fig: Figure = Figure(figsize=(10, 5))
        self.canvas: FigureCanvas = FigureCanvas(self.fig)

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle("IV残像ヒートマップ")
        self.resize(900, 500)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setMinimum(3)
        self.days_spin.setMaximum(90)
        self.days_spin.setValue(30)
        self.days_spin.setSuffix("日")

        self.month_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.month_combo.setFixedWidth(100)

        self.mode_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.mode_combo.addItems(["IV値 (年率%)", "前日比 (bp)"])

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("更新")
        button.clicked.connect(self.run_analysis)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("期間"))
        hbox.addWidget(self.days_spin)
        hbox.addWidget(QtWidgets.QLabel("限月"))
        hbox.addWidget(self.month_combo)
        hbox.addWidget(QtWidgets.QLabel("表示モード"))
        hbox.addWidget(self.mode_combo)
        hbox.addStretch()
        hbox.addWidget(button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.canvas)
        self.setLayout(vbox)

    def build_matrix(self, bars: list[BarData]) -> tuple[np.ndarray, list[str], list[str]]:
        """個別オプションバーからデルタレベル別×日付のIV行列を構築"""
        # Group bars by date
        date_bars: dict[str, list[BarData]] = {}
        for bar in bars:
            if bar.iv <= 0 or bar.delta == 0:
                continue
            date_key: str = bar.datetime.strftime("%Y-%m-%d")
            date_bars.setdefault(date_key, []).append(bar)

        sorted_dates: list[str] = sorted(date_bars.keys())
        if not sorted_dates:
            return np.array([]), [], []

        delta_labels: list[str] = [t[0] for t in self.DELTA_TARGETS]
        n_deltas: int = len(self.DELTA_TARGETS)
        n_dates: int = len(sorted_dates)
        matrix: np.ndarray = np.full((n_deltas, n_dates), np.nan)

        for j, date_key in enumerate(sorted_dates):
            day_bars: list[BarData] = date_bars[date_key]

            for i, (label, target_delta) in enumerate(self.DELTA_TARGETS):
                best_bar: BarData | None = None
                best_diff: float = float("inf")

                for bar in day_bars:
                    diff: float = abs(bar.delta - target_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best_bar = bar

                if best_bar and best_diff < 0.05:
                    matrix[i, j] = best_bar.iv * 100

        return matrix, sorted_dates, delta_labels

    def run_analysis(self) -> None:
        days: int = self.days_spin.value()
        mode: str = self.mode_combo.currentText()

        all_bars: list[BarData] = _load_option_bars_with_today(days)
        if not all_bars:
            QtWidgets.QMessageBox.warning(
                self,
                "データなし",
                f"過去{days}日間のオプションデータが見つかりません",
                QtWidgets.QMessageBox.Ok,
            )
            return

        _populate_month_combo(self.month_combo, all_bars)
        month: str = self.month_combo.currentText()
        bars: list[BarData] = _filter_bars_by_month(all_bars, month)

        matrix, date_labels, delta_labels = self.build_matrix(bars)
        if matrix.size == 0:
            return

        if "前日比" in mode:
            diff: np.ndarray = np.diff(matrix, axis=1)
            matrix = diff * 100
            date_labels = date_labels[1:]

        self.update_chart(matrix, date_labels, delta_labels, mode)

    def update_chart(
        self,
        matrix: np.ndarray,
        date_labels: list[str],
        delta_labels: list[str],
        mode: str,
    ) -> None:
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        masked: np.ma.MaskedArray = np.ma.masked_invalid(matrix)

        cmap: str = "RdYlBu_r" if "IV値" in mode else "RdBu_r"
        im = ax.pcolormesh(
            masked,
            cmap=cmap,
            edgecolors="grey",
            linewidth=0.3,
        )
        self.fig.colorbar(im, ax=ax, pad=0.02)

        # 各セルにIV値をテキスト表示
        n_rows, n_cols = matrix.shape
        for i in range(n_rows):
            for j in range(n_cols):
                val: float = matrix[i, j]
                if not np.isnan(val):
                    fontsize: int = 9 if n_cols <= 15 else 7 if n_cols <= 25 else 6
                    fmt: str = f"{val:.1f}" if "IV値" in mode else f"{val:+.0f}"
                    ax.text(
                        j + 0.5, i + 0.5, fmt,
                        ha="center", va="center",
                        fontsize=fontsize, color="black",
                        fontweight="bold",
                    )

        n_dates: int = len(date_labels)
        step: int = max(1, n_dates // 15)
        tick_positions: list[float] = [i + 0.5 for i in range(0, n_dates, step)]
        tick_labels: list[str] = [date_labels[i][5:] for i in range(0, n_dates, step)]
        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)

        ax.set_yticks([i + 0.5 for i in range(len(delta_labels))])
        ax.set_yticklabels(delta_labels, fontsize=9)

        ax.set_title("IV残像ヒートマップ", fontsize=12)
        ax.set_xlabel("日付")
        ax.set_ylabel("デルタレベル")

        self.fig.tight_layout()
        self.canvas.draw()


class IVDecayChart(QtWidgets.QWidget):
    """IV実績vs理論減衰チャート - イベント後のIV残像を定量化"""

    DELTA_TARGETS: list[tuple[str, float, str]] = [
        ("ATM (Δ0.50)", -0.50, "#ffffff"),
        ("Put Δ0.10", -0.10, "#ff8800"),
        ("Call Δ0.10", 0.10, "#00ccff"),
    ]

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name
        self.fig: Figure = Figure(figsize=(12, 6))
        self.canvas: FigureCanvas = FigureCanvas(self.fig)

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle("IV実績 vs 理論減衰")
        self.resize(1000, 600)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setMinimum(5)
        self.days_spin.setMaximum(90)
        self.days_spin.setValue(30)
        self.days_spin.setSuffix("日")

        self.event_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.event_combo.setFixedWidth(220)

        self.month_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.month_combo.setFixedWidth(100)

        self.delta_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for label, _delta, _color in self.DELTA_TARGETS:
            self.delta_combo.addItem(label)

        self.after_days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.after_days_spin.setMinimum(3)
        self.after_days_spin.setMaximum(30)
        self.after_days_spin.setValue(30)
        self.after_days_spin.setSuffix("日後")

        scan_button: QtWidgets.QPushButton = QtWidgets.QPushButton("イベント検出")
        scan_button.clicked.connect(self.scan_events)

        plot_button: QtWidgets.QPushButton = QtWidgets.QPushButton("描画")
        plot_button.clicked.connect(self.run_analysis)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("期間"))
        hbox.addWidget(self.days_spin)
        hbox.addWidget(QtWidgets.QLabel("限月"))
        hbox.addWidget(self.month_combo)
        hbox.addWidget(scan_button)
        hbox.addWidget(QtWidgets.QLabel("イベント日"))
        hbox.addWidget(self.event_combo)
        hbox.addWidget(QtWidgets.QLabel("デルタ"))
        hbox.addWidget(self.delta_combo)
        hbox.addWidget(QtWidgets.QLabel("表示"))
        hbox.addWidget(self.after_days_spin)
        hbox.addStretch()
        hbox.addWidget(plot_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.canvas)
        self.setLayout(vbox)

    def _build_daily_iv(
        self, bars: list[BarData], target_delta: float, month: str = "全て"
    ) -> dict[str, float]:
        """日付→IV(%) の辞書を構築"""
        filtered: list[BarData] = _filter_bars_by_month(bars, month)

        date_bars: dict[str, list[BarData]] = {}
        for bar in filtered:
            if bar.iv <= 0 or bar.delta == 0:
                continue
            date_key: str = bar.datetime.strftime("%Y-%m-%d")
            date_bars.setdefault(date_key, []).append(bar)

        daily_iv: dict[str, float] = {}
        for date_key in sorted(date_bars.keys()):
            best_bar: BarData | None = None
            best_diff: float = float("inf")
            for bar in date_bars[date_key]:
                diff: float = abs(bar.delta - target_delta)
                if diff < best_diff:
                    best_diff = diff
                    best_bar = bar
            if best_bar and best_diff < 0.05:
                daily_iv[date_key] = best_bar.iv * 100
        return daily_iv

    def scan_events(self) -> None:
        """IVスパイク（前日比が大きい日）を自動検出してcomboに追加"""
        days: int = self.days_spin.value()
        bars: list[BarData] = _load_option_bars_with_today(days)
        if not bars:
            return

        _populate_month_combo(self.month_combo, bars)
        month: str = self.month_combo.currentText()

        # ATMのIV時系列を構築
        daily_iv: dict[str, float] = self._build_daily_iv(bars, -0.50, month)
        sorted_dates: list[str] = sorted(daily_iv.keys())
        if len(sorted_dates) < 3:
            return

        # 前日比を計算してスパイクを検出
        spikes: list[tuple[str, float]] = []
        for i in range(1, len(sorted_dates)):
            prev_iv: float = daily_iv[sorted_dates[i - 1]]
            curr_iv: float = daily_iv[sorted_dates[i]]
            change: float = curr_iv - prev_iv
            if change > 0:
                spikes.append((sorted_dates[i], change))

        # 変化量の大きい順にソート
        spikes.sort(key=lambda x: x[1], reverse=True)

        self.event_combo.clear()
        for date_key, change in spikes[:15]:
            self.event_combo.addItem(f"{date_key} (+{change:.1f}%)")

    def run_analysis(self) -> None:
        event_text: str = self.event_combo.currentText()
        if not event_text:
            QtWidgets.QMessageBox.warning(
                self, "未選択", "先に「イベント検出」でイベント日を選択してください",
                QtWidgets.QMessageBox.Ok,
            )
            return

        event_date: str = event_text[:10]
        delta_idx: int = self.delta_combo.currentIndex()
        _label, target_delta, color = self.DELTA_TARGETS[delta_idx]
        month: str = self.month_combo.currentText()

        days: int = self.days_spin.value()
        bars: list[BarData] = _load_option_bars_with_today(days)
        if not bars:
            return

        daily_iv: dict[str, float] = self._build_daily_iv(bars, target_delta, month)
        sorted_dates: list[str] = sorted(daily_iv.keys())

        if event_date not in sorted_dates:
            return

        event_idx: int = sorted_dates.index(event_date)
        after_days: int = self.after_days_spin.value()
        # イベント前1日 + イベント日 + after_days
        start_idx: int = max(0, event_idx - 1)
        end_idx: int = min(len(sorted_dates), event_idx + after_days + 1)

        plot_dates: list[str] = sorted_dates[start_idx:end_idx]
        actual_ivs: list[float] = [daily_iv.get(d, float("nan")) for d in plot_dates]

        # イベント日のIVをピークとして理論減衰カーブ（√t）を計算
        peak_iv: float = daily_iv[event_date]
        # イベント前日のIVをベースラインとする
        if event_idx > 0:
            base_iv: float = daily_iv.get(sorted_dates[event_idx - 1], peak_iv * 0.8)
        else:
            base_iv = peak_iv * 0.8
        iv_spike: float = peak_iv - base_iv

        event_pos: int = event_idx - start_idx
        theory_ivs: list[float] = []
        for k in range(len(plot_dates)):
            t: int = k - event_pos  # イベント日からの日数
            if t < 0:
                theory_ivs.append(float("nan"))
            elif t == 0:
                theory_ivs.append(peak_iv)
            else:
                # 理論減衰: spike / √(t+1) + base
                decay: float = iv_spike / np.sqrt(t + 1)
                theory_ivs.append(base_iv + decay)

        self.update_chart(plot_dates, actual_ivs, theory_ivs, event_date, event_pos, _label, color)

    def update_chart(
        self,
        date_labels: list[str],
        actual_ivs: list[float],
        theory_ivs: list[float],
        event_date: str,
        event_pos: int,
        delta_label: str,
        color: str,
    ) -> None:
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        x = np.arange(len(date_labels))

        # 実績IV
        ax.plot(x, actual_ivs, color=color, linewidth=2, label=f"実績IV ({delta_label})",
                marker="o", markersize=5)

        # 各線分の中心に前日比テキストを表示
        for i in range(1, len(actual_ivs)):
            prev_val: float = actual_ivs[i - 1]
            curr_val: float = actual_ivs[i]
            if np.isnan(prev_val) or np.isnan(curr_val):
                continue
            diff_val: float = curr_val - prev_val
            mid_x: float = (x[i - 1] + x[i]) / 2
            mid_y: float = (prev_val + curr_val) / 2
            text_color: str = "#ff6666" if diff_val >= 0 else "#00ff99"
            ax.text(
                mid_x, mid_y, f"{diff_val:+.1f}",
                ha="center", va="bottom", fontsize=11,
                color=text_color, fontweight="bold",
            )

        # 理論減衰カーブ
        ax.plot(x, theory_ivs, color="#888888", linewidth=1.5, linestyle="--",
                label="理論減衰 (1/√t)", marker="", markersize=0)

        # 残像領域を塗りつぶし（実績が理論より高い部分）
        actual_arr = np.array(actual_ivs, dtype=float)
        theory_arr = np.array(theory_ivs, dtype=float)
        mask = ~(np.isnan(actual_arr) | np.isnan(theory_arr))
        if mask.any():
            ax.fill_between(
                x, actual_arr, theory_arr,
                where=mask & (actual_arr > theory_arr),
                alpha=0.3, color="#ff6666", label="IV残像",
            )

        # イベント日の縦線
        ax.axvline(x=event_pos, color="#ffff00", linewidth=1, linestyle=":", alpha=0.7)
        ax.text(event_pos, ax.get_ylim()[1], f" Event\n {event_date}",
                color="#ffff00", fontsize=8, va="top")

        ax.legend(loc="upper right", fontsize=9, framealpha=0.7)
        ax.grid(True, alpha=0.3)

        # X軸ラベル
        relative_labels: list[str] = []
        for i, d in enumerate(date_labels):
            offset: int = i - event_pos
            if offset == 0:
                relative_labels.append(f"E\n{d[5:]}")
            elif offset < 0:
                relative_labels.append(f"{offset}\n{d[5:]}")
            else:
                relative_labels.append(f"+{offset}\n{d[5:]}")

        ax.set_xticks(x)
        ax.set_xticklabels(relative_labels, fontsize=7, ha="center")

        ax.set_title(f"IV実績 vs 理論減衰 — {delta_label}", fontsize=12)
        ax.set_xlabel("イベント日からの日数")
        ax.set_ylabel("IV (年率%)")

        self.fig.tight_layout()
        self.canvas.draw()


class IVTimeSeriesChart(QtWidgets.QWidget):
    """IV時系列チャート - デルタレベル別IV推移の折れ線グラフ"""

    DELTA_TARGETS: list[tuple[str, float, str]] = [
        ("Put Δ0.02", -0.02, "#ff4444"),
        ("Put Δ0.10", -0.10, "#ff8800"),
        ("ATM (Δ0.50)", -0.50, "#ffffff"),
        ("Call Δ0.10", 0.10, "#00ccff"),
        ("Call Δ0.02", 0.02, "#44ff44"),
    ]

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name
        self.fig: Figure = Figure(figsize=(12, 6))
        self.canvas: FigureCanvas = FigureCanvas(self.fig)

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle("IV時系列チャート")
        self.resize(1000, 600)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setMinimum(3)
        self.days_spin.setMaximum(90)
        self.days_spin.setValue(30)
        self.days_spin.setSuffix("日")

        self.month_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.month_combo.setFixedWidth(100)

        self.delta_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.delta_combo.addItem("全デルタ")
        for label, _delta, _color in self.DELTA_TARGETS:
            self.delta_combo.addItem(label)

        self.ymin_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.ymin_spin.setMinimum(0)
        self.ymin_spin.setMaximum(200)
        self.ymin_spin.setValue(0)
        self.ymin_spin.setSuffix("%")
        self.ymin_spin.setDecimals(1)

        self.ymax_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.ymax_spin.setMinimum(0)
        self.ymax_spin.setMaximum(200)
        self.ymax_spin.setValue(0)
        self.ymax_spin.setSuffix("%")
        self.ymax_spin.setDecimals(1)

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("更新")
        button.clicked.connect(self.run_analysis)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("期間"))
        hbox.addWidget(self.days_spin)
        hbox.addWidget(QtWidgets.QLabel("限月"))
        hbox.addWidget(self.month_combo)
        hbox.addWidget(QtWidgets.QLabel("デルタ"))
        hbox.addWidget(self.delta_combo)
        hbox.addWidget(QtWidgets.QLabel("Y軸min"))
        hbox.addWidget(self.ymin_spin)
        hbox.addWidget(QtWidgets.QLabel("max"))
        hbox.addWidget(self.ymax_spin)
        hbox.addStretch()
        hbox.addWidget(button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.canvas)
        self.setLayout(vbox)

    def build_series(
        self, bars: list[BarData]
    ) -> tuple[list[str], dict[str, list[float]]]:
        """個別オプションバーからデルタレベル別IV時系列を構築"""
        date_bars: dict[str, list[BarData]] = {}
        for bar in bars:
            if bar.iv <= 0 or bar.delta == 0:
                continue
            date_key: str = bar.datetime.strftime("%Y-%m-%d")
            date_bars.setdefault(date_key, []).append(bar)

        sorted_dates: list[str] = sorted(date_bars.keys())
        if not sorted_dates:
            return [], {}

        series: dict[str, list[float]] = {}
        for label, target_delta, _color in self.DELTA_TARGETS:
            series[label] = []

        for date_key in sorted_dates:
            day_bars: list[BarData] = date_bars[date_key]

            for label, target_delta, _color in self.DELTA_TARGETS:
                best_bar: BarData | None = None
                best_diff: float = float("inf")

                for bar in day_bars:
                    diff: float = abs(bar.delta - target_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best_bar = bar

                if best_bar and best_diff < 0.05:
                    series[label].append(best_bar.iv * 100)
                else:
                    series[label].append(float("nan"))

        return sorted_dates, series

    def run_analysis(self) -> None:
        days: int = self.days_spin.value()

        all_bars: list[BarData] = _load_option_bars_with_today(days)
        if not all_bars:
            QtWidgets.QMessageBox.warning(
                self,
                "データなし",
                f"過去{days}日間のオプションデータが見つかりません",
                QtWidgets.QMessageBox.Ok,
            )
            return

        _populate_month_combo(self.month_combo, all_bars)
        month: str = self.month_combo.currentText()
        bars: list[BarData] = _filter_bars_by_month(all_bars, month)

        date_labels, series = self.build_series(bars)
        if not date_labels:
            return

        delta_selection: str = self.delta_combo.currentText()
        ymin: float = self.ymin_spin.value()
        ymax: float = self.ymax_spin.value()
        self.update_chart(date_labels, series, delta_selection, ymin, ymax)

    def update_chart(
        self,
        date_labels: list[str],
        series: dict[str, list[float]],
        delta_selection: str,
        ymin: float,
        ymax: float,
    ) -> None:
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        x = np.arange(len(date_labels))

        if delta_selection == "全デルタ":
            plot_targets = self.DELTA_TARGETS
        else:
            plot_targets = [t for t in self.DELTA_TARGETS if t[0] == delta_selection]

        for label, target_delta, color in plot_targets:
            values: list[float] = series[label]
            ax.plot(x, values, color=color, linewidth=1.5, label=label, marker=".", markersize=3)

            # 前日比テキストを表示
            fs: int = 11 if delta_selection != "全デルタ" else 9
            for i in range(1, len(values)):
                prev_val: float = values[i - 1]
                curr_val: float = values[i]
                if np.isnan(prev_val) or np.isnan(curr_val):
                    continue
                diff_val: float = curr_val - prev_val
                mid_x: float = (x[i - 1] + x[i]) / 2
                mid_y: float = (prev_val + curr_val) / 2
                ax.text(
                    mid_x, mid_y, f"{diff_val:+.1f}",
                    ha="center", va="bottom", fontsize=fs,
                    color=color, fontweight="bold",
                )

        ax.legend(loc="upper right", fontsize=8, framealpha=0.7)
        ax.grid(True, alpha=0.3)

        if ymin > 0 or ymax > 0:
            if ymin > 0 and ymax > 0 and ymax > ymin:
                ax.set_ylim(ymin, ymax)
            elif ymin > 0:
                ax.set_ylim(bottom=ymin)
            elif ymax > 0:
                ax.set_ylim(top=ymax)

        n_dates: int = len(date_labels)
        step: int = max(1, n_dates // 15)
        tick_positions = [i for i in range(0, n_dates, step)]
        tick_labels: list[str] = [date_labels[i][5:] for i in range(0, n_dates, step)]
        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)

        title: str = f"IV時系列 — {delta_selection}" if delta_selection != "全デルタ" else "IV時系列（デルタレベル別）"
        ax.set_title(title, fontsize=12)
        ax.set_xlabel("日付")
        ax.set_ylabel("IV (年率%)")

        self.fig.tight_layout()
        self.canvas.draw()