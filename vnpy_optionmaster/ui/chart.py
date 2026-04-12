from datetime import datetime, timedelta
import pyqtgraph as pg
from typing import cast

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ, get_database, BaseDatabase
from vnpy.trader.object import BarData

from vnpy.trader.utility import load_json, save_json

from ..base import PortfolioData, OptionData, PreviousDayOptionData
from ..engine import OptionEngine, Event, EventEngine
from ..time import ANNUAL_DAYS
from ..pricing import black_76

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

        # Crosshair cursor for impv_chart
        self.impv_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("gray", width=0.8, style=QtCore.Qt.DashLine))
        self.impv_hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("gray", width=0.8, style=QtCore.Qt.DashLine))
        self.impv_chart.addItem(self.impv_vline, ignoreBounds=True)
        self.impv_chart.addItem(self.impv_hline, ignoreBounds=True)
        self.impv_vline.hide()
        self.impv_hline.hide()

        self.impv_cursor_label = pg.TextItem(color="white", anchor=(0, 1))
        self.impv_cursor_label.setFont(QtGui.QFont("", 11))
        self.impv_chart.addItem(self.impv_cursor_label, ignoreBounds=True)
        self.impv_cursor_label.hide()

        self.impv_proxy = pg.SignalProxy(
            self.impv_chart.scene().sigMouseMoved, rateLimit=60, slot=self._on_impv_mouse_move
        )

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

        self.eris_p_strike_lines[chain_symbol].hide()
        self.eris_c_strike_lines[chain_symbol].hide()
        self.atm_strike_lines[chain_symbol].hide()
        self.underlying_price_lines[chain_symbol].hide()

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

    def _on_impv_mouse_move(self, evt) -> None:
        """Show crosshair and coordinate label on impv_chart mouse hover."""
        pos = evt[0]
        if not self.impv_chart.sceneBoundingRect().contains(pos):
            self.impv_vline.hide()
            self.impv_hline.hide()
            self.impv_cursor_label.hide()
            return

        mouse_point = self.impv_chart.vb.mapSceneToView(pos)
        x: float = mouse_point.x()
        y: float = mouse_point.y()

        self.impv_vline.setPos(x)
        self.impv_hline.setPos(y)
        self.impv_vline.show()
        self.impv_hline.show()

        self.impv_cursor_label.setText(f"権利行使価格: {x:.0f}  IV: {y:.2f}%")
        self.impv_cursor_label.setPos(x, y)
        self.impv_cursor_label.show()


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
        ("Put Δ0.10", -0.10),
        ("ATM (Δ0.50)", -0.50),
        ("Call Δ0.10", 0.10),
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
        ("Put Δ0.10", -0.10, "#ff8800"),
        ("ATM (Δ0.50)", -0.50, "#ffffff"),
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


class PayoffDiagramChart(QtWidgets.QWidget):
    """ペイオフ図 - Position payoff diagram with IV adjustment."""

    SETTING_FILENAME: str = "payoff_diagram_setting.json"

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name

        self.init_ui()
        self._load_settings()

    def init_ui(self) -> None:
        self.setWindowTitle("ペイオフ図")
        self.resize(1200, 900)

        # --- Mode selector ---
        self.mode_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.mode_combo.addItems(["ポートフォリオ", "シミュレーション"])
        self.mode_combo.currentIndexChanged.connect(self._toggle_mode)

        # --- Simulation panel (hidden by default) ---
        self.sim_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox("シミュレーション設定")

        self.sim_underlying_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.sim_underlying_spin.setRange(10000, 100000)
        self.sim_underlying_spin.setValue(57000)
        self.sim_underlying_spin.setSingleStep(100)

        self.sim_days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.sim_days_spin.setSuffix(" 日")
        self.sim_days_spin.setRange(1, 365)
        self.sim_days_spin.setValue(25)

        self.sim_rate_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.sim_rate_spin.setSuffix(" %")
        self.sim_rate_spin.setRange(0, 10)
        self.sim_rate_spin.setValue(0.0)
        self.sim_rate_spin.setDecimals(2)

        # Simulation position table
        self.sim_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget(0, 5)
        self.sim_table.setHorizontalHeaderLabels(
            ["種類", "行使価格", "枚数", "建値", "IV(%)"]
        )
        self.sim_table.horizontalHeader().setStretchLastSection(True)
        self.sim_table.setMaximumHeight(180)

        add_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("行追加")
        add_btn.clicked.connect(self._add_sim_row)
        del_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("行削除")
        del_btn.clicked.connect(self._remove_sim_row)

        sim_param_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        sim_param_hbox.addWidget(QtWidgets.QLabel("先物価格"))
        sim_param_hbox.addWidget(self.sim_underlying_spin)
        sim_param_hbox.addWidget(QtWidgets.QLabel("残存日数"))
        sim_param_hbox.addWidget(self.sim_days_spin)
        sim_param_hbox.addWidget(QtWidgets.QLabel("金利"))
        sim_param_hbox.addWidget(self.sim_rate_spin)
        sim_param_hbox.addStretch()
        sim_param_hbox.addWidget(add_btn)
        sim_param_hbox.addWidget(del_btn)

        sim_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        sim_layout.addLayout(sim_param_hbox)
        sim_layout.addWidget(self.sim_table)
        self.sim_group.setLayout(sim_layout)
        self.sim_group.hide()

        # --- Common controls ---
        self.price_range_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.price_range_spin.setSuffix(" pt")
        self.price_range_spin.setMinimum(1000)
        self.price_range_spin.setMaximum(20000)
        self.price_range_spin.setSingleStep(1000)
        self.price_range_spin.setValue(5000)

        self.step_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.step_combo.addItems(["500", "1000"])

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setSuffix(" 日")
        self.days_spin.setMinimum(0)
        self.days_spin.setMaximum(30)
        self.days_spin.setValue(1)

        self.iv_sensitivity_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.iv_sensitivity_spin.setSuffix(" %/1000pt")
        self.iv_sensitivity_spin.setMinimum(-5.0)
        self.iv_sensitivity_spin.setMaximum(5.0)
        self.iv_sensitivity_spin.setSingleStep(0.1)
        self.iv_sensitivity_spin.setValue(-1.0)
        self.iv_sensitivity_spin.setDecimals(1)

        self.show_legs_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("個別ポジション")
        self.show_legs_check.setChecked(True)

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("分析実行")
        button.clicked.connect(self.run_analysis)

        # Chart tabs (left side)
        self.chart_tabs: QtWidgets.QTabWidget = QtWidgets.QTabWidget()

        self.pnl_fig: Figure = Figure()
        self.pnl_canvas: FigureCanvas = FigureCanvas(self.pnl_fig)
        self.pnl_ax = self.pnl_fig.add_subplot(111)
        self.chart_tabs.addTab(self.pnl_canvas, "損益")

        self.delta_fig: Figure = Figure()
        self.delta_canvas: FigureCanvas = FigureCanvas(self.delta_fig)
        self.delta_ax = self.delta_fig.add_subplot(111)
        self.chart_tabs.addTab(self.delta_canvas, "Delta")

        self.gamma_fig: Figure = Figure()
        self.gamma_canvas: FigureCanvas = FigureCanvas(self.gamma_fig)
        self.gamma_ax = self.gamma_fig.add_subplot(111)
        self.chart_tabs.addTab(self.gamma_canvas, "Gamma")

        self.theta_fig: Figure = Figure()
        self.theta_canvas: FigureCanvas = FigureCanvas(self.theta_fig)
        self.theta_ax = self.theta_fig.add_subplot(111)
        self.chart_tabs.addTab(self.theta_canvas, "Theta")

        self.vega_fig: Figure = Figure()
        self.vega_canvas: FigureCanvas = FigureCanvas(self.vega_fig)
        self.vega_ax = self.vega_fig.add_subplot(111)
        self.chart_tabs.addTab(self.vega_canvas, "Vega")

        # Crosshair cursor setup — elements are created per-axis after each redraw
        self._cursor_lines: dict[str, tuple] = {}
        self._cursor_texts: dict[str, tuple] = {}
        self._cursor_y_fmts: dict[str, str] = {
            "pnl": ".1f", "delta": ".2f", "gamma": ".6f", "theta": ".1f", "vega": ".2f",
        }
        for canvas in (self.pnl_canvas, self.delta_canvas,
                       self.gamma_canvas, self.theta_canvas, self.vega_canvas):
            canvas.mpl_connect("motion_notify_event", self._on_mouse_move)

        # Results table (right side, below settings)
        self.result_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.result_table.setAlternatingRowColors(True)

        # --- Right panel: all settings + table ---
        right_panel: QtWidgets.QWidget = QtWidgets.QWidget()
        right_panel.setMaximumWidth(2880)
        right_panel.setMinimumWidth(350)

        # Mode selector
        mode_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        mode_hbox.addWidget(QtWidgets.QLabel("モード"))
        mode_hbox.addWidget(self.mode_combo)
        mode_hbox.addStretch()

        # Common controls — use a form-style grid
        ctrl_grid: QtWidgets.QGridLayout = QtWidgets.QGridLayout()
        ctrl_grid.addWidget(QtWidgets.QLabel("価格範囲"), 0, 0)
        ctrl_grid.addWidget(self.price_range_spin, 0, 1)
        ctrl_grid.addWidget(QtWidgets.QLabel("ステップ"), 0, 2)
        ctrl_grid.addWidget(self.step_combo, 0, 3)
        ctrl_grid.addWidget(QtWidgets.QLabel("日数"), 1, 0)
        ctrl_grid.addWidget(self.days_spin, 1, 1)
        ctrl_grid.addWidget(QtWidgets.QLabel("IV感応度"), 1, 2)
        ctrl_grid.addWidget(self.iv_sensitivity_spin, 1, 3)

        btn_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        btn_hbox.addWidget(self.show_legs_check)
        btn_hbox.addStretch()
        btn_hbox.addWidget(button)

        right_vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        right_vbox.addLayout(mode_hbox)
        right_vbox.addWidget(self.sim_group)
        right_vbox.addLayout(ctrl_grid)
        right_vbox.addLayout(btn_hbox)
        right_vbox.addWidget(self.result_table, stretch=1)
        right_panel.setLayout(right_vbox)

        # --- Main layout: chart (left) | settings+table (right) ---
        splitter: QtWidgets.QSplitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.chart_tabs)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 6)

        main_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        main_layout.addWidget(splitter)
        self.setLayout(main_layout)

    # ------------------------------------------------------------------
    #  Mouse cursor crosshair
    # ------------------------------------------------------------------
    def _init_cursor(self, name: str, ax) -> None:
        """(Re-)create crosshair elements on an axes after it has been redrawn."""
        # Preserve axis limits so the cursor elements don't alter them
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()

        h_line = ax.axhline(color="gray", linewidth=0.8, linestyle="--", visible=False)
        v_line = ax.axvline(color="gray", linewidth=0.8, linestyle="--", visible=False)
        self._cursor_lines[name] = (h_line, v_line)

        bbox_style = dict(facecolor="black", alpha=0.7, edgecolor="none", pad=3)
        x_text = ax.text(
            xlim[0], ylim[0], "", fontsize=11, color="white", bbox=bbox_style,
            visible=False, transform=ax.transData,
        )
        y_text = ax.text(
            xlim[0], ylim[0], "", fontsize=11, color="white", bbox=bbox_style,
            visible=False, transform=ax.transData,
        )
        self._cursor_texts[name] = (x_text, y_text, self._cursor_y_fmts[name])

        # Restore original limits
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

    def _on_mouse_move(self, event) -> None:
        """Show crosshair and x/y labels on mouse hover for all charts."""
        if not event.inaxes:
            for h, v in self._cursor_lines.values():
                h.set_visible(False)
                v.set_visible(False)
            for xt, yt, _ in self._cursor_texts.values():
                xt.set_visible(False)
                yt.set_visible(False)
            for canvas in (self.pnl_canvas, self.delta_canvas,
                           self.gamma_canvas, self.theta_canvas,
                           self.vega_canvas):
                canvas.draw_idle()
            return

        ax = event.inaxes
        x, y = event.xdata, event.ydata

        # Find which chart this event belongs to
        name_map = {
            id(self.pnl_ax): ("pnl", self.pnl_canvas),
            id(self.delta_ax): ("delta", self.delta_canvas),
            id(self.gamma_ax): ("gamma", self.gamma_canvas),
            id(self.theta_ax): ("theta", self.theta_canvas),
            id(self.vega_ax): ("vega", self.vega_canvas),
        }
        match = name_map.get(id(ax))
        if not match:
            return
        name, canvas = match

        if name not in self._cursor_lines:
            return

        h_line, v_line = self._cursor_lines[name]
        x_text, y_text, y_fmt = self._cursor_texts[name]

        h_line.set_ydata([y, y])
        v_line.set_xdata([x, x])
        h_line.set_visible(True)
        v_line.set_visible(True)

        xlim = ax.get_xlim()
        ylim = ax.get_ylim()

        x_text.set_position((x, ylim[0]))
        x_text.set_text(f"{x:.0f}")
        x_text.set_ha("center")
        x_text.set_va("top")
        x_text.set_visible(True)

        y_text.set_position((xlim[0], y))
        y_text.set_text(f"{y:{y_fmt}}")
        y_text.set_ha("left")
        y_text.set_va("center")
        y_text.set_visible(True)

        canvas.draw_idle()

    # ------------------------------------------------------------------
    #  Save / Load settings
    # ------------------------------------------------------------------
    def _save_settings(self) -> None:
        """Save all settings and simulation positions to JSON."""
        # Read simulation position rows
        sim_positions: list[dict] = []
        for row in range(self.sim_table.rowCount()):
            combo: QtWidgets.QComboBox = self.sim_table.cellWidget(row, 0)
            type_text: str = combo.currentText() if combo else "コール"
            sim_positions.append({
                "type": type_text,
                "strike": self.sim_table.item(row, 1).text() if self.sim_table.item(row, 1) else "",
                "lots": self.sim_table.item(row, 2).text() if self.sim_table.item(row, 2) else "",
                "entry_price": self.sim_table.item(row, 3).text() if self.sim_table.item(row, 3) else "",
                "iv": self.sim_table.item(row, 4).text() if self.sim_table.item(row, 4) else "",
            })

        data: dict = {
            "mode": self.mode_combo.currentIndex(),
            "sim_underlying": self.sim_underlying_spin.value(),
            "sim_days": self.sim_days_spin.value(),
            "sim_rate": self.sim_rate_spin.value(),
            "sim_positions": sim_positions,
            "price_range": self.price_range_spin.value(),
            "step": self.step_combo.currentText(),
            "days": self.days_spin.value(),
            "iv_sensitivity": self.iv_sensitivity_spin.value(),
            "show_legs": self.show_legs_check.isChecked(),
        }
        save_json(self.SETTING_FILENAME, data)

    def _load_settings(self) -> None:
        """Restore settings and simulation positions from JSON."""
        data: dict = load_json(self.SETTING_FILENAME)
        if not data:
            return

        self.mode_combo.setCurrentIndex(data.get("mode", 0))
        self.sim_underlying_spin.setValue(data.get("sim_underlying", 57000))
        self.sim_days_spin.setValue(data.get("sim_days", 25))
        self.sim_rate_spin.setValue(data.get("sim_rate", 0.0))
        self.price_range_spin.setValue(data.get("price_range", 5000))
        self.days_spin.setValue(data.get("days", 1))
        self.iv_sensitivity_spin.setValue(data.get("iv_sensitivity", -1.0))
        self.show_legs_check.setChecked(data.get("show_legs", True))

        step_text: str = data.get("step", "500")
        idx: int = self.step_combo.findText(step_text)
        if idx >= 0:
            self.step_combo.setCurrentIndex(idx)

        # Restore simulation position rows
        for pos in data.get("sim_positions", []):
            row: int = self.sim_table.rowCount()
            self.sim_table.insertRow(row)

            type_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
            type_combo.addItems(["コール", "プット", "先物ミニ", "先物ラージ"])
            combo_idx: int = type_combo.findText(pos.get("type", "コール"))
            if combo_idx >= 0:
                type_combo.setCurrentIndex(combo_idx)
            self.sim_table.setCellWidget(row, 0, type_combo)

            self.sim_table.setItem(row, 1, QtWidgets.QTableWidgetItem(pos.get("strike", "")))
            self.sim_table.setItem(row, 2, QtWidgets.QTableWidgetItem(pos.get("lots", "")))
            self.sim_table.setItem(row, 3, QtWidgets.QTableWidgetItem(pos.get("entry_price", "")))
            self.sim_table.setItem(row, 4, QtWidgets.QTableWidgetItem(pos.get("iv", "")))

        self._toggle_mode()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Auto-save settings when the window is closed."""
        self._save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    #  Simulation panel helpers
    # ------------------------------------------------------------------
    def _toggle_mode(self) -> None:
        is_sim: bool = self.mode_combo.currentIndex() == 1
        self.sim_group.setVisible(is_sim)

    def _add_sim_row(self) -> None:
        row: int = self.sim_table.rowCount()
        self.sim_table.insertRow(row)

        type_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        type_combo.addItems(["コール", "プット", "先物ミニ", "先物ラージ"])
        self.sim_table.setCellWidget(row, 0, type_combo)

        self.sim_table.setItem(row, 1, QtWidgets.QTableWidgetItem("62000"))
        self.sim_table.setItem(row, 2, QtWidgets.QTableWidgetItem("5"))
        self.sim_table.setItem(row, 3, QtWidgets.QTableWidgetItem("152"))
        self.sim_table.setItem(row, 4, QtWidgets.QTableWidgetItem("25.0"))

    def _remove_sim_row(self) -> None:
        row: int = self.sim_table.currentRow()
        if row >= 0:
            self.sim_table.removeRow(row)

    def _get_sim_positions(self) -> list[dict]:
        """Read positions from the simulation table."""
        type_map: dict[str, tuple[str, int]] = {
            "コール": ("call", 1000),
            "プット": ("put", 1000),
            "先物ミニ": ("futures", 100),
            "先物ラージ": ("futures", 1000),
        }
        positions: list[dict] = []
        for row in range(self.sim_table.rowCount()):
            combo: QtWidgets.QComboBox = self.sim_table.cellWidget(row, 0)
            if not combo:
                continue
            type_text: str = combo.currentText()
            kind, size = type_map.get(type_text, ("call", 1000))

            try:
                strike = float(self.sim_table.item(row, 1).text()) if kind != "futures" else 0.0
                lots = int(float(self.sim_table.item(row, 2).text()))
                entry_price = float(self.sim_table.item(row, 3).text())
                iv = float(self.sim_table.item(row, 4).text()) / 100.0 if kind != "futures" else 0.0
            except (ValueError, AttributeError):
                continue

            cp: int = 0
            if kind == "call":
                cp = 1
            elif kind == "put":
                cp = -1

            positions.append({
                "kind": kind, "cp": cp, "strike": strike,
                "lots": lots, "entry_price": entry_price,
                "iv": iv, "size": size, "label": type_text,
            })
        return positions

    def _calculate_sim_pnl_at_price(
        self,
        positions: list[dict],
        sim_price: float,
        ref_price: float,
        time_to_expiry: float,
        interest_rate: float,
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[float, float, float, float, dict[int, float]]:
        """Calculate P&L for simulation positions (change from current state).

        Baseline is the model-calculated value at ref_price, so P&L = 0
        at (ref_price, same time, same IV).
        """
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)

        total_now: float = 0.0
        total_day_same: float = 0.0
        total_day_adj: float = 0.0
        total_expiry: float = 0.0
        leg_pnls: dict[int, float] = {}

        for i, pos in enumerate(positions):
            lots: int = pos["lots"]
            size: int = pos["size"]

            if pos["kind"] == "futures":
                # Baseline = ref_price; P&L = 0 when sim_price == ref_price
                pnl: float = (sim_price - ref_price) * lots * size
                total_now += pnl
                total_day_same += pnl
                total_day_adj += pnl
                total_expiry += pnl
                leg_pnls[i] = pnl
            else:
                cp: int = pos["cp"]
                strike: float = pos["strike"]
                iv: float = pos["iv"]

                # Baseline: model price at current conditions
                base: float = black_76.calculate_price(
                    ref_price, strike, interest_rate, time_to_expiry, iv, cp
                )

                # Scenario 1: current time, current IV
                p_now: float = black_76.calculate_price(
                    sim_price, strike, interest_rate, time_to_expiry, iv, cp
                )
                total_now += (p_now - base) * lots * size

                # Scenario 2: +N days, same IV
                new_t: float = max(time_to_expiry - time_change, 1e-6)
                p_day: float = black_76.calculate_price(
                    sim_price, strike, interest_rate, new_t, iv, cp
                )
                total_day_same += (p_day - base) * lots * size

                # Scenario 3: +N days, IV adjusted
                new_iv: float = max(iv + iv_adj, 0.01)
                p_adj: float = black_76.calculate_price(
                    sim_price, strike, interest_rate, new_t, new_iv, cp
                )
                total_day_adj += (p_adj - base) * lots * size

                # Scenario 4: expiration
                intrinsic: float = max(0.0, cp * (sim_price - strike))
                total_expiry += (intrinsic - base) * lots * size

                leg_pnls[i] = (p_adj - base) * lots * size

        return total_now, total_day_same, total_day_adj, total_expiry, leg_pnls

    def _calculate_pnl_at_price(
        self,
        portfolio: PortfolioData,
        sim_price: float,
        ref_price: float,
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[float, float, float, float, dict[str, float]]:
        """Calculate portfolio P&L at a simulated underlying price.

        Returns (pnl_now, pnl_day_same_iv, pnl_day_adj_iv, pnl_expiry, leg_pnls)
        in raw yen.
        """
        price_ratio: float = sim_price / ref_price
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)

        total_now: float = 0.0
        total_day_same: float = 0.0
        total_day_adj: float = 0.0
        total_expiry: float = 0.0
        leg_pnls: dict[str, float] = {}

        # Underlying (futures) P&L — same for all time scenarios
        for underlying in portfolio.underlyings.values():
            if not underlying.net_pos:
                continue
            pnl: float = (underlying.mid_price * price_ratio - underlying.mid_price) \
                * underlying.net_pos * underlying.size
            total_now += pnl
            total_day_same += pnl
            total_day_adj += pnl
            total_expiry += pnl
            leg_pnls[underlying.vt_symbol] = pnl

        # Option P&L
        for option in portfolio.options.values():
            if not option.net_pos or not option.mid_impv or not option.tick:
                continue

            current_last: float = option.tick.last_price
            if not current_last:
                continue

            new_underlying: float = option.underlying.mid_price * price_ratio
            multiplier: float = option.net_pos * option.size

            # Scenario 1: current time, current IV
            try:
                p_now, _, _, _, _ = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    option.time_to_expiry, option.mid_impv, option.option_type
                )
            except Exception:
                p_now = current_last
            total_now += (p_now - current_last) * multiplier

            # Scenario 2: +N days, same IV
            new_t: float = max(option.time_to_expiry - time_change, 1e-6)
            try:
                p_day, _, _, _, _ = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    new_t, option.mid_impv, option.option_type
                )
            except Exception:
                p_day = current_last
            total_day_same += (p_day - current_last) * multiplier

            # Scenario 3: +N days, IV adjusted by underlying move
            new_iv: float = max(option.mid_impv + iv_adj, 0.01)
            try:
                p_adj, _, _, _, _ = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    new_t, new_iv, option.option_type
                )
            except Exception:
                p_adj = current_last
            total_day_adj += (p_adj - current_last) * multiplier

            # Scenario 4: at expiration (intrinsic only)
            intrinsic: float = max(
                0.0, option.option_type * (new_underlying - option.strike_price)
            )
            total_expiry += (intrinsic - current_last) * multiplier

            # Leg P&L uses the IV-adjusted scenario
            leg_pnls[option.vt_symbol] = (p_adj - current_last) * multiplier

        return total_now, total_day_same, total_day_adj, total_expiry, leg_pnls

    def run_analysis(self) -> None:
        is_sim: bool = self.mode_combo.currentIndex() == 1

        if is_sim:
            self._run_sim_analysis()
        else:
            self._run_portfolio_analysis()

    # ------------------------------------------------------------------
    #  Portfolio mode
    # ------------------------------------------------------------------
    def _run_portfolio_analysis(self) -> None:
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        # Find reference underlying with valid price
        ref_price: float = 0.0
        for underlying in portfolio.underlyings.values():
            if underlying.mid_price:
                ref_price = underlying.mid_price
                break

        if not ref_price:
            QtWidgets.QMessageBox.warning(
                self, "エラー",
                "原資産の価格データがありません",
                QtWidgets.QMessageBox.Ok
            )
            return

        # Read parameters
        price_range: int = self.price_range_spin.value()
        step: int = int(self.step_combo.currentText())
        days: int = self.days_spin.value()
        iv_per_1000: float = self.iv_sensitivity_spin.value()
        time_change: float = days / ANNUAL_DAYS
        show_legs: bool = self.show_legs_check.isChecked()

        # Build leg labels
        leg_labels: dict[str, str] = {}
        if show_legs:
            for underlying in portfolio.underlyings.values():
                if underlying.net_pos:
                    pos_str = f"+{underlying.net_pos}" if underlying.net_pos > 0 \
                        else str(underlying.net_pos)
                    leg_labels[underlying.vt_symbol] = f"先物 {pos_str}枚"
            for option in portfolio.options.values():
                if option.net_pos and option.mid_impv and option.tick:
                    cp_str = "C" if option.option_type > 0 else "P"
                    pos_str = f"+{option.net_pos}" if option.net_pos > 0 \
                        else str(option.net_pos)
                    leg_labels[option.vt_symbol] = \
                        f"{cp_str}{option.strike_price:.0f} {pos_str}枚"

        # Fine-grained prices for smooth chart curves
        fine_step: int = 100
        prices_fine: np.ndarray = np.arange(
            ref_price - price_range,
            ref_price + price_range + fine_step,
            fine_step,
        )

        pnl_now_arr: list[float] = []
        pnl_day_same_arr: list[float] = []
        pnl_day_adj_arr: list[float] = []
        pnl_expiry_arr: list[float] = []
        leg_pnl_arrs: dict[str, list[float]] = {k: [] for k in leg_labels}
        greeks_data: dict[str, dict[str, list[float]]] = {
            k: {"now": [], "day_same": [], "day_adj": []}
            for k in ("delta", "gamma", "theta", "vega")
        }

        for sim_price in prices_fine:
            sp: float = float(sim_price)
            now, day_same, day_adj, expiry, legs = self._calculate_pnl_at_price(
                portfolio, sp, ref_price, time_change, iv_per_1000
            )
            pnl_now_arr.append(now / 1000)
            pnl_day_same_arr.append(day_same / 1000)
            pnl_day_adj_arr.append(day_adj / 1000)
            pnl_expiry_arr.append(expiry / 1000)

            for key in leg_pnl_arrs:
                leg_pnl_arrs[key].append(legs.get(key, 0) / 1000)

            g_now, g_ds, g_da = self._calculate_portfolio_greeks_at_price(
                portfolio, sp, ref_price, time_change, iv_per_1000
            )
            for gk in ("delta", "gamma", "theta", "vega"):
                greeks_data[gk]["now"].append(g_now[gk])
                greeks_data[gk]["day_same"].append(g_ds[gk])
                greeks_data[gk]["day_adj"].append(g_da[gk])

        # Step-level values for the results table
        prices_step: np.ndarray = np.arange(
            ref_price - price_range,
            ref_price + price_range + step,
            step,
        )
        table_data: list[dict] = []
        for sim_price in prices_step:
            now, day_same, day_adj, expiry, _ = self._calculate_pnl_at_price(
                portfolio, float(sim_price), ref_price, time_change, iv_per_1000
            )
            iv_change_pct: float = iv_per_1000 * ((sim_price - ref_price) / 1000.0)
            table_data.append({
                "price": float(sim_price),
                "iv_change": iv_change_pct,
                "pnl_now": now / 1000,
                "pnl_day_same": day_same / 1000,
                "pnl_day_adj": day_adj / 1000,
                "pnl_expiry": expiry / 1000,
            })

        # Portfolio greeks for title
        greeks_str: str = (
            f"Δ:{portfolio.pos_delta:.2f}  Γ:{portfolio.pos_gamma:.2f}  "
            f"Θ:{portfolio.pos_theta:.2f}  V:{portfolio.pos_vega:.2f}"
        )

        self._update_chart(
            prices_fine, pnl_now_arr, pnl_day_same_arr, pnl_day_adj_arr,
            pnl_expiry_arr, leg_pnl_arrs, leg_labels, ref_price, days,
            show_legs, greeks_str,
        )
        self._update_greeks_charts(prices_fine, greeks_data, ref_price, days)
        self._update_table(table_data, days)

    # ------------------------------------------------------------------
    #  Simulation mode
    # ------------------------------------------------------------------
    def _run_sim_analysis(self) -> None:
        positions: list[dict] = self._get_sim_positions()
        if not positions:
            QtWidgets.QMessageBox.warning(
                self, "エラー",
                "ポジションを入力してください",
                QtWidgets.QMessageBox.Ok
            )
            return

        ref_price: float = float(self.sim_underlying_spin.value())
        sim_days_to_expiry: int = self.sim_days_spin.value()
        time_to_expiry: float = sim_days_to_expiry / ANNUAL_DAYS
        interest_rate: float = self.sim_rate_spin.value() / 100.0

        price_range: int = self.price_range_spin.value()
        step: int = int(self.step_combo.currentText())
        days: int = self.days_spin.value()
        iv_per_1000: float = self.iv_sensitivity_spin.value()
        time_change: float = days / ANNUAL_DAYS
        show_legs: bool = self.show_legs_check.isChecked()

        # Build leg labels from simulation positions
        leg_labels: dict[int, str] = {}
        if show_legs:
            for i, pos in enumerate(positions):
                sign: str = "+" if pos["lots"] > 0 else ""
                if pos["kind"] == "futures":
                    leg_labels[i] = f"{pos['label']} {sign}{pos['lots']}枚"
                else:
                    cp_str = "C" if pos["cp"] == 1 else "P"
                    leg_labels[i] = \
                        f"{cp_str}{pos['strike']:.0f} {sign}{pos['lots']}枚"

        # Fine-grained prices
        fine_step: int = 100
        prices_fine: np.ndarray = np.arange(
            ref_price - price_range,
            ref_price + price_range + fine_step,
            fine_step,
        )

        pnl_now_arr: list[float] = []
        pnl_day_same_arr: list[float] = []
        pnl_day_adj_arr: list[float] = []
        pnl_expiry_arr: list[float] = []
        leg_pnl_arrs: dict[int, list[float]] = {k: [] for k in leg_labels}
        greeks_data: dict[str, dict[str, list[float]]] = {
            k: {"now": [], "day_same": [], "day_adj": []}
            for k in ("delta", "gamma", "theta", "vega")
        }

        for sim_price in prices_fine:
            sp: float = float(sim_price)
            now, day_same, day_adj, expiry, legs = self._calculate_sim_pnl_at_price(
                positions, sp, ref_price,
                time_to_expiry, interest_rate, time_change, iv_per_1000,
            )
            pnl_now_arr.append(now / 1000)
            pnl_day_same_arr.append(day_same / 1000)
            pnl_day_adj_arr.append(day_adj / 1000)
            pnl_expiry_arr.append(expiry / 1000)

            for key in leg_pnl_arrs:
                leg_pnl_arrs[key].append(legs.get(key, 0) / 1000)

            g_now, g_ds, g_da = self._calculate_sim_greeks_at_price(
                positions, sp, ref_price,
                time_to_expiry, interest_rate, time_change, iv_per_1000,
            )
            for gk in ("delta", "gamma", "theta", "vega"):
                greeks_data[gk]["now"].append(g_now[gk])
                greeks_data[gk]["day_same"].append(g_ds[gk])
                greeks_data[gk]["day_adj"].append(g_da[gk])

        # Step-level values for table
        prices_step: np.ndarray = np.arange(
            ref_price - price_range,
            ref_price + price_range + step,
            step,
        )
        table_data: list[dict] = []
        for sim_price in prices_step:
            now, day_same, day_adj, expiry, _ = self._calculate_sim_pnl_at_price(
                positions, float(sim_price), ref_price,
                time_to_expiry, interest_rate, time_change, iv_per_1000,
            )
            iv_change_pct: float = iv_per_1000 * ((sim_price - ref_price) / 1000.0)
            table_data.append({
                "price": float(sim_price),
                "iv_change": iv_change_pct,
                "pnl_now": now / 1000,
                "pnl_day_same": day_same / 1000,
                "pnl_day_adj": day_adj / 1000,
                "pnl_expiry": expiry / 1000,
            })

        # Greeks summary for simulation
        greeks_str = self._calc_sim_greeks_str(
            positions, ref_price, time_to_expiry, interest_rate
        )

        self._update_chart(
            prices_fine, pnl_now_arr, pnl_day_same_arr, pnl_day_adj_arr,
            pnl_expiry_arr, leg_pnl_arrs, leg_labels, ref_price, days,
            show_legs, greeks_str,
        )
        self._update_greeks_charts(prices_fine, greeks_data, ref_price, days)
        self._update_table(table_data, days)

    def _calc_sim_greeks_str(
        self,
        positions: list[dict],
        ref_price: float,
        time_to_expiry: float,
        interest_rate: float,
    ) -> str:
        """Calculate aggregate greeks for simulation positions."""
        total_delta: float = 0.0
        total_gamma: float = 0.0
        total_theta: float = 0.0
        total_vega: float = 0.0

        for pos in positions:
            lots: int = pos["lots"]
            size: int = pos["size"]
            if pos["kind"] == "futures":
                total_delta += lots * size
            else:
                _, delta, gamma, theta, vega = black_76.calculate_greeks(
                    ref_price, pos["strike"], interest_rate,
                    time_to_expiry, pos["iv"], pos["cp"],
                )
                total_delta += delta * size * lots
                total_gamma += gamma * size * lots
                total_theta += theta * size / ANNUAL_DAYS * lots
                total_vega += vega * size / 100 * lots

        return (
            f"Δ:{total_delta:.2f}  Γ:{total_gamma:.2f}  "
            f"Θ:{total_theta:.2f}  V:{total_vega:.2f}"
        )

    # ------------------------------------------------------------------
    #  Chart rendering
    # ------------------------------------------------------------------
    def _update_chart(
        self,
        prices: np.ndarray,
        pnl_now: list[float],
        pnl_day_same: list[float],
        pnl_day_adj: list[float],
        pnl_expiry: list[float],
        leg_pnl_arrs: dict,
        leg_labels: dict,
        current_price: float,
        days: int,
        show_legs: bool,
        greeks_str: str = "",
    ) -> None:
        self.pnl_ax.clear()

        # Individual position legs (thin, semi-transparent)
        if show_legs and leg_pnl_arrs:
            leg_colors = [
                "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
                "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
            ]
            for i, (key, pnls) in enumerate(leg_pnl_arrs.items()):
                color = leg_colors[i % len(leg_colors)]
                label = leg_labels.get(key, str(key))
                self.pnl_ax.plot(
                    prices, pnls, color=color, linewidth=1, alpha=0.6, label=label,
                )

        # Main scenario curves
        self.pnl_ax.plot(prices, pnl_now, color="white", linewidth=2.5, label="現在")
        self.pnl_ax.plot(
            prices, pnl_day_same, color="#00BFFF", linewidth=2,
            label=f"+{days}日(同IV)",
        )
        self.pnl_ax.plot(
            prices, pnl_day_adj, color="#FFD700", linewidth=2,
            label=f"+{days}日(IV変動)",
        )
        self.pnl_ax.plot(
            prices, pnl_expiry, color="#808080", linewidth=1,
            linestyle="--", label="満期",
        )

        # Reference lines
        self.pnl_ax.axvline(
            x=current_price, color="cyan", linestyle=":", alpha=0.5,
            label=f"現在値: {current_price:.0f}",
        )
        self.pnl_ax.axhline(y=0, color="gray", linestyle="-", alpha=0.3)

        title: str = "損益"
        if greeks_str:
            title += f"    {greeks_str}"
        self.pnl_ax.set_title(title)
        self.pnl_ax.set_xlabel("原資産価格")
        self.pnl_ax.set_ylabel("損益 (千円)")
        self.pnl_ax.legend(loc="upper right", fontsize=8, framealpha=0.7)
        self.pnl_ax.grid(True, alpha=0.3)

        self._init_cursor("pnl", self.pnl_ax)
        self.pnl_fig.tight_layout()
        self.pnl_canvas.draw()

    # ------------------------------------------------------------------
    #  Greeks calculation
    # ------------------------------------------------------------------
    def _calculate_sim_greeks_at_price(
        self,
        positions: list[dict],
        sim_price: float,
        ref_price: float,
        time_to_expiry: float,
        interest_rate: float,
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Calculate aggregate Greeks at a simulated price for simulation mode.

        Returns (greeks_now, greeks_day_same, greeks_day_adj).
        Each dict has keys: delta, gamma, theta, vega.
        """
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)
        g_now: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_same: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_adj: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        for pos in positions:
            lots: int = pos["lots"]
            size: int = pos["size"]

            if pos["kind"] == "futures":
                fd: float = lots * size
                g_now["delta"] += fd
                g_day_same["delta"] += fd
                g_day_adj["delta"] += fd
            else:
                cp: int = pos["cp"]
                strike: float = pos["strike"]
                iv: float = pos["iv"]
                new_t: float = max(time_to_expiry - time_change, 1e-6)
                new_iv: float = max(iv + iv_adj, 0.01)

                # Current time, current IV
                _, d, g, th, ve = black_76.calculate_greeks(
                    sim_price, strike, interest_rate, time_to_expiry, iv, cp
                )
                g_now["delta"] += d * size * lots
                g_now["gamma"] += g * size * lots
                g_now["theta"] += th * size / ANNUAL_DAYS * lots
                g_now["vega"] += ve * size / 100 * lots

                # +N days, same IV
                _, d, g, th, ve = black_76.calculate_greeks(
                    sim_price, strike, interest_rate, new_t, iv, cp
                )
                g_day_same["delta"] += d * size * lots
                g_day_same["gamma"] += g * size * lots
                g_day_same["theta"] += th * size / ANNUAL_DAYS * lots
                g_day_same["vega"] += ve * size / 100 * lots

                # +N days, IV adjusted
                _, d, g, th, ve = black_76.calculate_greeks(
                    sim_price, strike, interest_rate, new_t, new_iv, cp
                )
                g_day_adj["delta"] += d * size * lots
                g_day_adj["gamma"] += g * size * lots
                g_day_adj["theta"] += th * size / ANNUAL_DAYS * lots
                g_day_adj["vega"] += ve * size / 100 * lots

        return g_now, g_day_same, g_day_adj

    def _calculate_portfolio_greeks_at_price(
        self,
        portfolio: PortfolioData,
        sim_price: float,
        ref_price: float,
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Calculate aggregate Greeks at a simulated price for portfolio mode."""
        price_ratio: float = sim_price / ref_price
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)
        g_now: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_same: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_adj: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        for underlying in portfolio.underlyings.values():
            if not underlying.net_pos:
                continue
            fd: float = underlying.size * underlying.net_pos
            g_now["delta"] += fd
            g_day_same["delta"] += fd
            g_day_adj["delta"] += fd

        for option in portfolio.options.values():
            if not option.net_pos or not option.mid_impv or not option.tick:
                continue
            new_underlying: float = option.underlying.mid_price * price_ratio
            new_t: float = max(option.time_to_expiry - time_change, 1e-6)
            new_iv: float = max(option.mid_impv + iv_adj, 0.01)
            m: int = option.net_pos

            try:
                _, d, ga, th, ve = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    option.time_to_expiry, option.mid_impv, option.option_type
                )
                g_now["delta"] += d * option.size * m
                g_now["gamma"] += ga * option.size * m
                g_now["theta"] += th * option.size / ANNUAL_DAYS * m
                g_now["vega"] += ve * option.size / 100 * m
            except Exception:
                pass

            try:
                _, d, ga, th, ve = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    new_t, option.mid_impv, option.option_type
                )
                g_day_same["delta"] += d * option.size * m
                g_day_same["gamma"] += ga * option.size * m
                g_day_same["theta"] += th * option.size / ANNUAL_DAYS * m
                g_day_same["vega"] += ve * option.size / 100 * m
            except Exception:
                pass

            try:
                _, d, ga, th, ve = option.calculate_greeks(
                    new_underlying, option.strike_price, option.interest_rate,
                    new_t, new_iv, option.option_type
                )
                g_day_adj["delta"] += d * option.size * m
                g_day_adj["gamma"] += ga * option.size * m
                g_day_adj["theta"] += th * option.size / ANNUAL_DAYS * m
                g_day_adj["vega"] += ve * option.size / 100 * m
            except Exception:
                pass

        return g_now, g_day_same, g_day_adj

    # ------------------------------------------------------------------
    #  Greeks chart rendering
    # ------------------------------------------------------------------
    def _update_greeks_charts(
        self,
        prices: np.ndarray,
        greeks_data: dict[str, dict[str, list[float]]],
        current_price: float,
        days: int,
    ) -> None:
        """Plot Delta, Gamma, Theta charts.

        greeks_data structure:
          {"delta": {"now": [...], "day_same": [...], "day_adj": [...]},
           "gamma": {...}, "theta": {...}}
        """
        # Unit conversion factors and Y-axis labels
        #   delta: yen/pt  → 千円/pt  (/ 1000)
        #   gamma: yen/pt/pt → 千円/pt² (/ 1000)
        #   theta: yen/day  → 千円/day  (/ 1000)
        conv: dict[str, float] = {
            "delta": 1.0 / 1000.0,
            "gamma": 1.0 / 1000.0,
            "theta": 1.0 / 1000.0,
            "vega": 1.0 / 1000.0,
        }
        ylabel: dict[str, str] = {
            "delta": "Delta (千円/1pt)",
            "gamma": "Gamma (千円/1pt²)",
            "theta": "Theta (千円/day)",
            "vega": "Vega (千円/1%IV)",
        }

        chart_info: list[tuple] = [
            (self.delta_ax, self.delta_fig, self.delta_canvas, "delta", "Delta"),
            (self.gamma_ax, self.gamma_fig, self.gamma_canvas, "gamma", "Gamma"),
            (self.theta_ax, self.theta_fig, self.theta_canvas, "theta", "Theta"),
            (self.vega_ax, self.vega_fig, self.vega_canvas, "vega", "Vega"),
        ]

        for ax, fig, canvas, key, title in chart_info:
            ax.clear()
            series: dict[str, list[float]] = greeks_data[key]
            c: float = conv[key]

            ax.plot(
                prices, [v * c for v in series["now"]],
                color="white", linewidth=2.5, label="現在",
            )
            ax.plot(
                prices, [v * c for v in series["day_same"]],
                color="#00BFFF", linewidth=2, label=f"+{days}日(同IV)",
            )
            ax.plot(
                prices, [v * c for v in series["day_adj"]],
                color="#FFD700", linewidth=2, label=f"+{days}日(IV変動)",
            )

            ax.axvline(
                x=current_price, color="cyan", linestyle=":", alpha=0.5,
            )
            ax.axhline(y=0, color="gray", linestyle="-", alpha=0.3)

            # Set tick label decimal places per Greek
            decimals: dict[str, int] = {"delta": 2, "gamma": 6, "theta": 1, "vega": 2}
            fmt: str = f"%.{decimals[key]}f"
            ax.yaxis.set_major_formatter(plt.FormatStrFormatter(fmt))

            ax.set_title(title)
            ax.set_xlabel("原資産価格")
            ax.set_ylabel(ylabel[key])
            ax.legend(loc="upper right", fontsize=8, framealpha=0.7)
            ax.grid(True, alpha=0.3)

            self._init_cursor(key, ax)
            fig.tight_layout()
            canvas.draw()

    # ------------------------------------------------------------------
    #  Results table
    # ------------------------------------------------------------------
    def _update_table(self, table_data: list[dict], days: int) -> None:
        headers: list[str] = [
            "原資産価格", "IV変動(%)",
            "現在P&L", f"+{days}日(同IV)", f"+{days}日(IV変動)", "満期P&L",
        ]
        self.result_table.setColumnCount(len(headers))
        self.result_table.setRowCount(len(table_data))
        self.result_table.setHorizontalHeaderLabels(headers)

        for row, data in enumerate(table_data):
            values: list[str] = [
                f"{data['price']:.0f}",
                f"{data['iv_change']:+.1f}",
                f"{data['pnl_now']:.1f}",
                f"{data['pnl_day_same']:.1f}",
                f"{data['pnl_day_adj']:.1f}",
                f"{data['pnl_expiry']:.1f}",
            ]
            for col, val in enumerate(values):
                item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(val)
                item.setTextAlignment(
                    QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
                # Colour P&L columns: red for negative, green for positive
                if col >= 2:
                    try:
                        num: float = float(val)
                        if num < 0:
                            item.setForeground(QtGui.QColor(255, 100, 100))
                        elif num > 0:
                            item.setForeground(QtGui.QColor(100, 255, 100))
                    except ValueError:
                        pass
                self.result_table.setItem(row, col, item)

        self.result_table.resizeColumnsToContents()