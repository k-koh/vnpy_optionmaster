from datetime import datetime, timedelta
from collections import Counter, deque
import os
import re
import math
import pyqtgraph as pg
from typing import cast

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ, get_database, BaseDatabase
from vnpy.trader.object import BarData

from vnpy.trader.utility import load_json, save_json

from ..base import PortfolioData, OptionData, PreviousDayOptionData, ChainData, UnderlyingData
from ..engine import OptionEngine, Event, EventEngine
from ..time import ANNUAL_DAYS


import numpy as np
import matplotlib
matplotlib.use('Qt5Agg')                    # noqa
import matplotlib.pyplot as plt             # noqa
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas  # noqa
from matplotlib.figure import Figure        # noqa
from matplotlib.patches import Patch        # noqa
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
        self.delta002_p_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.delta002_c_strike_lines: dict[str, pg.InfiniteLine] = {}

        self.atm_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.underlying_price_lines: dict[str, pg.InfiniteLine] = {}
        self.prev_underlying_price_lines: dict[str, pg.InfiniteLine] = {}
        self.prev_underlying_prices: dict[str, float] = {}
        self._prev_underlying_loaded_date: str | None = None
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
        prev_underlying_line_pen = pg.mkPen(color=(255, 165, 0), width=2, style=QtCore.Qt.DashLine)
        position = self.underlying_line_positions.pop(0)
        prev_position = max(0.05, position - 0.08)

        # Stagger label positions per chain so month1/month2 strike-line labels
        # don't overlay each other when the lines sit at similar strikes.
        chain_index = len(self.eris_p_strike_lines)
        label_shift = chain_index * 0.08

        self.eris_p_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=p_line_pen,
            label=symbol + " プットΔ0.1",
            labelOpts={'position': max(0.05, 0.95 - label_shift), 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )
        self.eris_c_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=c_line_pen,
            label=symbol + " コールΔ0.1",
            labelOpts={'position': min(0.95, 0.01 + label_shift), 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )

        self.delta002_p_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=p_line_pen,
            label=symbol + " プットΔ0.02",
            labelOpts={'position': max(0.05, 0.92 - label_shift), 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )
        self.delta002_c_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=c_line_pen,
            label=symbol + " コールΔ0.02",
            labelOpts={'position': min(0.95, 0.04 + label_shift), 'color': color, 'fill': (200,200,200,50), 'movable': False}
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
        self.prev_underlying_price_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=prev_underlying_line_pen,
            label=symbol + " 前日先物",
            labelOpts={'position': prev_position, 'color': (255, 165, 0), 'fill': (200, 200, 200, 50), 'movable': False}
        )

        self.impv_chart.addItem(self.eris_p_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.eris_c_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.delta002_p_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.delta002_c_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.atm_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.underlying_price_lines[chain_symbol])
        self.impv_chart.addItem(self.prev_underlying_price_lines[chain_symbol])

        self.eris_p_strike_lines[chain_symbol].hide()
        self.eris_c_strike_lines[chain_symbol].hide()
        self.delta002_p_strike_lines[chain_symbol].hide()
        self.delta002_c_strike_lines[chain_symbol].hide()
        self.atm_strike_lines[chain_symbol].hide()
        self.underlying_price_lines[chain_symbol].hide()
        self.prev_underlying_price_lines[chain_symbol].hide()

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
                spread_text = f" 差{chain.eris_p_spread:.0f}" if chain.eris_p_spread is not None else ""
                line.label.setText(f"{symbol} PΔ{delta_text} IV{iv_text} ¥{price_text}{spread_text}")
                line.show()
            else:
                self.eris_p_strike_lines[chain.chain_symbol].hide()

            if chain.eris_c_strike is not None:
                line = self.eris_c_strike_lines[chain.chain_symbol]
                line.setPos(chain.eris_c_strike)
                delta_text = f"{chain.eris_c_delta:.3f}" if chain.eris_c_delta is not None else "N/A"
                iv_text = f"{chain.eris_c_iv:.3f}" if chain.eris_c_iv is not None else "N/A"
                price_text = f"{chain.eris_c_price:.0f}" if chain.eris_c_price is not None else "N/A"
                spread_text = f" 差{chain.eris_c_spread:.0f}" if chain.eris_c_spread is not None else ""
                line.label.setText(f"{symbol} CΔ{delta_text} IV{iv_text} ¥{price_text}{spread_text}")
                line.show()
            else:
                self.eris_c_strike_lines[chain.chain_symbol].hide()

            if chain.delta002_p_strike is not None:
                line = self.delta002_p_strike_lines[chain.chain_symbol]
                line.setPos(chain.delta002_p_strike)
                delta_text = f"{chain.delta002_p_delta:.3f}" if chain.delta002_p_delta is not None else "N/A"
                iv_text = f"{chain.delta002_p_iv:.3f}" if chain.delta002_p_iv is not None else "N/A"
                price_text = f"{chain.delta002_p_price:.0f}" if chain.delta002_p_price is not None else "N/A"
                spread_text = f" 差{chain.delta002_p_spread:.0f}" if chain.delta002_p_spread is not None else ""
                line.label.setText(f"{symbol} PΔ{delta_text} IV{iv_text} ¥{price_text}{spread_text}")
                line.show()
            else:
                self.delta002_p_strike_lines[chain.chain_symbol].hide()

            if chain.delta002_c_strike is not None:
                line = self.delta002_c_strike_lines[chain.chain_symbol]
                line.setPos(chain.delta002_c_strike)
                delta_text = f"{chain.delta002_c_delta:.3f}" if chain.delta002_c_delta is not None else "N/A"
                iv_text = f"{chain.delta002_c_iv:.3f}" if chain.delta002_c_iv is not None else "N/A"
                price_text = f"{chain.delta002_c_price:.0f}" if chain.delta002_c_price is not None else "N/A"
                spread_text = f" 差{chain.delta002_c_spread:.0f}" if chain.delta002_c_spread is not None else ""
                line.label.setText(f"{symbol} CΔ{delta_text} IV{iv_text} ¥{price_text}{spread_text}")
                line.show()
            else:
                self.delta002_c_strike_lines[chain.chain_symbol].hide()


            # Update ATM strike line
            if chain.atm_price:
                self.atm_strike_lines[chain.chain_symbol].setPos(chain.atm_price)
                self.atm_strike_lines[chain.chain_symbol].show()
            else:
                self.atm_strike_lines[chain.chain_symbol].hide()

            # Load previous day futures close first (needed for the 先物 line's
            # 前日差 label below and for the 前日先物 line).
            today_key: str = datetime.now(DB_TZ).strftime("%Y-%m-%d")
            if self._prev_underlying_loaded_date != today_key:
                self.prev_underlying_prices.clear()
                self._prev_underlying_loaded_date = today_key

            if chain.chain_symbol not in self.prev_underlying_prices:
                prev_price = self._load_prev_day_futures_close(chain.chain_symbol)
                if prev_price is not None:
                    self.prev_underlying_prices[chain.chain_symbol] = prev_price

            prev_price = self.prev_underlying_prices.get(chain.chain_symbol)

            # Update underlying (current futures) price line
            underlying_price = None
            if calls:
                underlying_price = calls[0].underlying.mid_price
            elif puts:
                underlying_price = puts[0].underlying.mid_price

            if underlying_price:
                line = self.underlying_price_lines[chain.chain_symbol]
                line.setPos(underlying_price)
                symbol = chain.chain_symbol.split(".")[0]
                label_text: str = f"{symbol} 先物: {underlying_price:.0f}"
                if prev_price:
                    diff: float = underlying_price - prev_price
                    label_text += f" (前日差: {diff:+.0f})"
                line.label.setText(label_text)
                line.show()
            else:
                self.underlying_price_lines[chain.chain_symbol].hide()

            # Update previous day futures close line
            if prev_price:
                prev_line = self.prev_underlying_price_lines[chain.chain_symbol]
                prev_line.setPos(prev_price)
                symbol = chain.chain_symbol.split(".")[0]
                prev_line.label.setText(f"{symbol} 前日先物: {prev_price:.0f}")
                prev_line.show()
            else:
                self.prev_underlying_price_lines[chain.chain_symbol].hide()

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
            d002_p_line = self.delta002_p_strike_lines[chain_symbol]
            d002_c_line = self.delta002_c_strike_lines[chain_symbol]
            atm_line = self.atm_strike_lines[chain_symbol]
            underlying_line = self.underlying_price_lines[chain_symbol]
            prev_underlying_line = self.prev_underlying_price_lines[chain_symbol]
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
                d002_p_line.hide()
                d002_c_line.hide()

                atm_line.hide()
                underlying_line.hide()
                prev_underlying_line.hide()
                total_volume_bar.hide()
                iv_diff_pos_bar.hide()
                iv_diff_neg_bar.hide()
                for text_item in self.iv_diff_pos_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.iv_diff_neg_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.total_volume_text_items[chain_symbol]:
                    text_item.hide()

    def _load_prev_day_futures_close(self, chain_symbol: str) -> float | None:
        """Load previous day's futures close price for a chain.

        chain_symbol is like "nk-202506.JPX"; futures symbol is "nk-202506".
        Reads pre_close directly from the most recent bar.
        """
        symbol: str = chain_symbol.split(".")[0]
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=2)

        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_bar_data(
            symbol=symbol,
            exchange=Exchange.JPX,
            interval=Interval.MINUTE,
            start=start,
            end=now,
        )
        if not bars:
            return None

        pre_close = bars[-1].pre_close
        return pre_close if pre_close else None

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


def _interpolate_iv_at_delta(
    day_bars: list[BarData], target_delta: float
) -> float | None:
    """
    Linearly interpolate IV at exactly target_delta.

    Picks the two same-side option bars (puts for negative target, calls for positive)
    whose deltas bracket target_delta, then interpolates IV between them.
    Returns the IV as a fraction (multiply by 100 for percent) or None when the
    target cannot be bracketed (no extrapolation outside the chain).
    """
    if target_delta < 0:
        side_bars: list[BarData] = [b for b in day_bars if b.delta < 0]
    elif target_delta > 0:
        side_bars = [b for b in day_bars if b.delta > 0]
    else:
        return None

    if not side_bars:
        return None

    side_bars.sort(key=lambda b: b.delta)

    lo_bar: BarData | None = None
    hi_bar: BarData | None = None
    for bar in side_bars:
        if bar.delta <= target_delta:
            lo_bar = bar
        if bar.delta >= target_delta:
            hi_bar = bar
            break

    # Don't extrapolate when target is outside the available delta range.
    if lo_bar is None or hi_bar is None:
        return None

    if lo_bar is hi_bar or hi_bar.delta == lo_bar.delta:
        return lo_bar.iv

    t: float = (target_delta - lo_bar.delta) / (hi_bar.delta - lo_bar.delta)
    return lo_bar.iv + t * (hi_bar.iv - lo_bar.iv)


class IVDecayChart(QtWidgets.QWidget):
    """IV実績vs理論減衰チャート - イベント後のIV残像を定量化"""

    SETTING_FILENAME: str = "iv_decay_chart_setting.json"

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

        # Cursor state
        self._cursor_vline = None
        self._cursor_annot = None
        self._cursor_date_labels: list[str] = []
        self._cursor_actual_ivs: list[float] = []
        self._cursor_theory_ivs: list[float] = []
        self._cursor_ax = None
        self._cursor_cid = None

        self.init_ui()
        self._load_settings()

    def _save_settings(self) -> None:
        data: dict = {
            "window_width": self.width(),
            "window_height": self.height(),
        }
        save_json(self.SETTING_FILENAME, data)

    def _load_settings(self) -> None:
        data: dict = load_json(self.SETTING_FILENAME)
        if not data:
            return
        win_w: int = data.get("window_width", 0)
        win_h: int = data.get("window_height", 0)
        if win_w > 0 and win_h > 0:
            self.resize(win_w, win_h)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save_settings()
        super().closeEvent(event)

    def init_ui(self) -> None:
        self.setWindowTitle("IV実績 vs 理論減衰")
        self.resize(1000, 600)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setMinimum(5)
        self.days_spin.setMaximum(90)
        self.days_spin.setValue(45)
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

        # Setup cursor
        self._cursor_ax = ax
        self._cursor_date_labels = date_labels
        self._cursor_actual_ivs = actual_ivs
        self._cursor_theory_ivs = theory_ivs

        self._cursor_vline = ax.axvline(x=0, color="#ffffff", linewidth=0.5, linestyle="--", alpha=0.5, visible=False)
        self._cursor_annot = ax.annotate(
            "", xy=(0, 0), xytext=(15, 15),
            textcoords="offset points",
            fontsize=8,
            color="#ffffff",
            bbox=dict(boxstyle="round,pad=0.3", fc="#333333", ec="#888888", alpha=0.9),
        )
        self._cursor_annot.set_visible(False)

        if self._cursor_cid:
            self.canvas.mpl_disconnect(self._cursor_cid)
        self._cursor_cid = self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)

        self.fig.tight_layout()
        self.canvas.draw()

    def _on_mouse_move(self, event) -> None:
        if event.inaxes is None or self._cursor_ax is None:
            if self._cursor_vline:
                self._cursor_vline.set_visible(False)
            if self._cursor_annot:
                self._cursor_annot.set_visible(False)
            self.canvas.draw_idle()
            return

        ix: int = int(round(event.xdata))
        if ix < 0 or ix >= len(self._cursor_date_labels):
            self._cursor_vline.set_visible(False)
            self._cursor_annot.set_visible(False)
            self.canvas.draw_idle()
            return

        self._cursor_vline.set_xdata([ix])
        self._cursor_vline.set_visible(True)

        date_str: str = self._cursor_date_labels[ix]
        lines: list[str] = [date_str]

        if ix < len(self._cursor_actual_ivs) and not np.isnan(self._cursor_actual_ivs[ix]):
            lines.append(f"実績IV: {self._cursor_actual_ivs[ix]:.1f}%")
        if ix < len(self._cursor_theory_ivs) and not np.isnan(self._cursor_theory_ivs[ix]):
            lines.append(f"理論IV: {self._cursor_theory_ivs[ix]:.1f}%")

        self._cursor_annot.set_text("\n".join(lines))
        self._cursor_annot.xy = (ix, event.ydata)
        self._cursor_annot.set_visible(True)

        self.canvas.draw_idle()


class IVTimeSeriesChart(QtWidgets.QWidget):
    """IV時系列チャート - デルタレベル別IV推移の折れ線グラフ"""

    SETTING_FILENAME: str = "iv_timeseries_chart_setting.json"

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

        # Cursor state
        self._cursor_vline = None
        self._cursor_text = None
        self._cursor_date_labels: list[str] = []
        self._cursor_series: dict[str, list[float]] = {}
        self._cursor_futures_values: list[float] = []
        self._cursor_plot_targets: list[tuple[str, float, str]] = []
        self._cursor_ax = None
        self._cursor_cid = None

        self.init_ui()
        self._load_settings()

    def _save_settings(self) -> None:
        data: dict = {
            "window_width": self.width(),
            "window_height": self.height(),
            "show_decay": self.decay_check.isChecked(),
            "show_envelope": self.envelope_check.isChecked(),
            "show_skew": self.skew_check.isChecked(),
            "show_pinned": self.pinned_check.isChecked(),
            "interval": self.interval_combo.currentText(),
            "auto_refresh": self.auto_refresh_check.isChecked(),
            "refresh_interval": self.refresh_interval_spin.value(),
        }
        save_json(self.SETTING_FILENAME, data)

    def _load_settings(self) -> None:
        data: dict = load_json(self.SETTING_FILENAME)
        if not data:
            return
        win_w: int = data.get("window_width", 0)
        win_h: int = data.get("window_height", 0)
        if win_w > 0 and win_h > 0:
            self.resize(win_w, win_h)
        self.decay_check.setChecked(data.get("show_decay", False))
        self.envelope_check.setChecked(data.get("show_envelope", False))
        self.skew_check.setChecked(data.get("show_skew", False))
        self.pinned_check.setChecked(data.get("show_pinned", False))
        self.interval_combo.setCurrentText(data.get("interval", "4H"))
        self.refresh_interval_spin.setValue(data.get("refresh_interval", 5))
        # Setting this checked starts the timer via _on_auto_refresh_toggled.
        self.auto_refresh_check.setChecked(data.get("auto_refresh", False))

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        """Start/stop the auto-refresh timer when the checkbox is toggled."""
        if checked:
            self._refresh_timer.start(self.refresh_interval_spin.value() * 60 * 1000)
        else:
            self._refresh_timer.stop()

    def _auto_refresh(self) -> None:
        """Timer slot: refresh silently, and only while the window is open.

        The widget is constructed (hidden) at OptionMaster start, so the
        restored 自動更新 setting can start the timer before the chart is ever
        opened. Skip when not visible, and never pop modal warnings for a
        background refresh (e.g. month = 全て with an intraday 時間足)."""
        if not self.isVisible():
            return
        self.run_analysis(show_warnings=False)

    def _on_refresh_interval_changed(self, minutes: int) -> None:
        """Apply a new interval immediately if auto-refresh is active."""
        if self.auto_refresh_check.isChecked():
            self._refresh_timer.start(minutes * 60 * 1000)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._refresh_timer.stop()
        self._save_settings()
        super().closeEvent(event)

    def init_ui(self) -> None:
        self.setWindowTitle("IV時系列チャート")
        self.resize(1000, 600)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setMinimum(3)
        self.days_spin.setMaximum(90)
        self.days_spin.setValue(45)
        self.days_spin.setSuffix("日")

        self.month_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.month_combo.setFixedWidth(100)

        # 時間足: 1D uses per-strike option chain (current behaviour); intraday
        # intervals resample the futures minute-bar IV (ATM / eris Δ0.1).
        self.interval_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.interval_combo.addItems(["1H", "2H", "4H", "8H", "12H", "1D"])
        self.interval_combo.setCurrentText("4H")

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

        self.decay_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("理論減衰")
        self.envelope_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("残像")
        self.skew_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("スキュー")
        self.pinned_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("ピン留め(固定行使価格)")

        # Auto-refresh: interval (minutes) + enable checkbox, before 更新 button
        self.refresh_interval_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.refresh_interval_spin.setMinimum(1)
        self.refresh_interval_spin.setMaximum(120)
        self.refresh_interval_spin.setValue(5)
        self.refresh_interval_spin.setSuffix("分")
        self.refresh_interval_spin.setFixedWidth(60)
        self.refresh_interval_spin.setToolTip("自動更新の間隔（分）")
        self.auto_refresh_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("自動更新")

        self._refresh_timer: QtCore.QTimer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._auto_refresh)
        self.auto_refresh_check.toggled.connect(self._on_auto_refresh_toggled)
        self.refresh_interval_spin.valueChanged.connect(self._on_refresh_interval_changed)

        # Timestamp of the last refresh (manual or auto), to confirm updates
        self.last_update_label: QtWidgets.QLabel = QtWidgets.QLabel("最終更新: ---")

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("更新")
        button.clicked.connect(self.run_analysis)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("期間"))
        hbox.addWidget(self.days_spin)
        hbox.addWidget(QtWidgets.QLabel("限月"))
        hbox.addWidget(self.month_combo)
        hbox.addWidget(QtWidgets.QLabel("時間足"))
        hbox.addWidget(self.interval_combo)
        hbox.addWidget(QtWidgets.QLabel("デルタ"))
        hbox.addWidget(self.delta_combo)
        hbox.addWidget(QtWidgets.QLabel("Y軸min"))
        hbox.addWidget(self.ymin_spin)
        hbox.addWidget(QtWidgets.QLabel("max"))
        hbox.addWidget(self.ymax_spin)
        hbox.addStretch()
        hbox.addWidget(self.decay_check)
        hbox.addWidget(self.envelope_check)
        hbox.addWidget(self.skew_check)
        hbox.addWidget(self.pinned_check)
        hbox.addWidget(self.refresh_interval_spin)
        hbox.addWidget(self.auto_refresh_check)
        hbox.addWidget(button)
        hbox.addWidget(self.last_update_label)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        # stretch=1 so the canvas takes all extra vertical space and the
        # toolbar row stays at its natural (minimal) height.
        vbox.addWidget(self.canvas, 1)
        self.setLayout(vbox)

    def build_series(
        self, bars: list[BarData]
    ) -> tuple[list[str], dict[str, list[float]]]:
        """個別オプションバーからデルタレベル別IV時系列を構築 (delta-interpolated)."""
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
                iv: float | None = _interpolate_iv_at_delta(day_bars, target_delta)
                if iv is not None and iv > 0:
                    series[label].append(iv * 100)
                else:
                    series[label].append(float("nan"))

        return sorted_dates, series

    def build_strike_anchored_series(
        self, bars: list[BarData], date_labels: list[str]
    ) -> tuple[dict[str, list[float]], dict[str, str]]:
        """Pin one option contract per Δ-target on the latest date and follow its IV.

        For each delta target, the contract whose delta is closest to the target
        on the most-recent valid date is selected ("anchor"). Its IV is then
        looked up by symbol on every prior date, so the series tracks one and
        only one contract — futures movement no longer changes which option is
        being measured.

        Returns:
            (pinned_series, anchor_symbols) where pinned_series[label] is the IV
            (in %) per date_label and anchor_symbols[label] is the pinned
            contract's symbol (e.g. "nk-2606-P-54000").
        """
        date_bars: dict[str, list[BarData]] = {}
        for bar in bars:
            if bar.iv <= 0 or bar.delta == 0 or not bar.symbol:
                continue
            date_key: str = bar.datetime.strftime("%Y-%m-%d")
            date_bars.setdefault(date_key, []).append(bar)

        return self._anchor_and_follow(date_bars, date_labels)

    def _anchor_and_follow(
        self, date_bars: dict[str, list[BarData]], date_labels: list[str]
    ) -> tuple[dict[str, list[float]], dict[str, str]]:
        """Anchor one contract per Δ-target on the latest bucket, follow its IV.

        `date_bars` maps each x-axis bucket key to that bucket's per-strike bars
        (one representative bar per contract). Works for both daily and intraday
        buckets. Returns (pinned_series, anchor_symbols) aligned to date_labels.
        """
        if not date_bars or not date_labels:
            return {}, {}

        # Anchor on the latest bucket that actually has bars.
        anchor_date: str | None = None
        for d in reversed(date_labels):
            if d in date_bars:
                anchor_date = d
                break
        if anchor_date is None:
            return {}, {}

        anchor_day_bars: list[BarData] = date_bars[anchor_date]

        anchor_symbols: dict[str, str] = {}
        for label, target_delta, _color in self.DELTA_TARGETS:
            if target_delta < 0:
                side_bars = [b for b in anchor_day_bars if b.delta < 0]
            elif target_delta > 0:
                side_bars = [b for b in anchor_day_bars if b.delta > 0]
            else:
                continue
            if not side_bars:
                continue
            best = min(side_bars, key=lambda b: abs(b.delta - target_delta))
            anchor_symbols[label] = best.symbol

        pinned_series: dict[str, list[float]] = {}
        for label, sym in anchor_symbols.items():
            values: list[float] = []
            for date_key in date_labels:
                day_bars = date_bars.get(date_key, [])
                hit: BarData | None = None
                for bar in day_bars:
                    if bar.symbol == sym:
                        hit = bar
                        break
                if hit is not None and hit.iv > 0:
                    values.append(hit.iv * 100)
                else:
                    values.append(float("nan"))
            pinned_series[label] = values

        return pinned_series, anchor_symbols

    def _build_intraday_pinned(
        self, month: str, days: int, hours: int, date_labels: list[str]
    ) -> tuple[dict[str, list[float]], dict[str, str]]:
        """Pinned (固定行使価格) series for intraday, from the 15m per-strike
        option bars, bucketed to the chosen `hours`-hour interval and aligned to
        date_labels (the futures-based main-series buckets)."""
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=days)
        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_option_data(
            symbol="", exchange=Exchange.JPX,
            interval=Interval.MINUTE15, start=start, end=now,
        )
        month_bars: list[BarData] = _filter_bars_by_month(bars, month)

        # Bucket by N-hour; keep the LATEST 15m bar per contract in each bucket.
        # (load_option_data orders by symbol, not datetime, so compare dt.)
        bucket_syms: dict[str, dict[str, BarData]] = {}
        for bar in month_bars:
            if bar.iv <= 0 or bar.delta == 0 or not bar.symbol:
                continue
            bh: int = bar.datetime.hour - bar.datetime.hour % hours
            key: str = bar.datetime.replace(
                hour=bh, minute=0, second=0, microsecond=0
            ).strftime("%Y-%m-%d %H:%M")
            syms = bucket_syms.setdefault(key, {})
            prev = syms.get(bar.symbol)
            if prev is None or bar.datetime > prev.datetime:
                syms[bar.symbol] = bar

        date_bars: dict[str, list[BarData]] = {
            k: list(v.values()) for k, v in bucket_syms.items()
        }
        return self._anchor_and_follow(date_bars, date_labels)

    def _build_intraday_series(
        self, month: str, days: int, hours: int
    ) -> tuple[list[str], dict[str, list[float]], dict[str, tuple[float, float, float, float]]]:
        """Resample futures minute-bar IV to `hours`-hour buckets.

        Uses the ATM / eris Δ0.1 IV recorded on the underlying futures minute
        bars (the only intraday IV available). Returns (labels, series,
        futures_ohlc), where series keys match the ATM / Put Δ0.10 / Call Δ0.10
        DELTA_TARGETS and IVs are annualized %.
        """
        symbol: str = f"nk-{month}"
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=days)
        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_bar_data(
            symbol=symbol, exchange=Exchange.JPX,
            interval=Interval.MINUTE, start=start, end=now,
        )
        if not bars:
            return [], {}, {}

        # Bucket by N-hour floor; keep OHLC + the bucket's last IV snapshot.
        buckets: dict[str, dict] = {}
        for bar in bars:
            bh: int = bar.datetime.hour - bar.datetime.hour % hours
            bdt = bar.datetime.replace(hour=bh, minute=0, second=0, microsecond=0)
            key: str = bdt.strftime("%Y-%m-%d %H:%M")
            b = buckets.get(key)
            if b is None:
                buckets[key] = {
                    "o": bar.open_price, "h": bar.high_price,
                    "l": bar.low_price, "c": bar.close_price,
                    "atm": bar.atm_iv, "ep": bar.eris_p_iv, "ec": bar.eris_c_iv,
                }
            else:
                b["h"] = max(b["h"], bar.high_price)
                b["l"] = min(b["l"], bar.low_price)
                b["c"] = bar.close_price
                # last non-zero IV snapshot in the bucket
                if bar.atm_iv:
                    b["atm"] = bar.atm_iv
                if bar.eris_p_iv:
                    b["ep"] = bar.eris_p_iv
                if bar.eris_c_iv:
                    b["ec"] = bar.eris_c_iv

        labels: list[str] = sorted(buckets.keys())

        def _iv_list(field: str) -> list[float]:
            out: list[float] = []
            for k in labels:
                v = buckets[k].get(field)
                out.append(v * 100.0 if v else float("nan"))
            return out

        series: dict[str, list[float]] = {
            "ATM (Δ0.50)": _iv_list("atm"),
            "Put Δ0.10": _iv_list("ep"),
            "Call Δ0.10": _iv_list("ec"),
        }
        futures_ohlc: dict[str, tuple[float, float, float, float]] = {
            k: (buckets[k]["o"], buckets[k]["h"], buckets[k]["l"], buckets[k]["c"])
            for k in labels
        }
        return labels, series, futures_ohlc

    def _load_futures_daily_ohlc(
        self, month: str, days: int
    ) -> dict[str, tuple[float, float, float, float]]:
        """Load futures 1-min bars for nk-{month} and aggregate to daily OHLC.

        Returns {date_str: (open, high, low, close)} e.g.
        {"2026-04-08": (57000.0, 57400.0, 56800.0, 57250.0)}.
        Night session (17:00+) belongs to the next day's session, so a session
        spans from the previous day 17:00 to that day's ~15:40 close.
        """
        symbol: str = f"nk-{month}"
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=days)
        end: datetime = now

        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_bar_data(
            symbol=symbol,
            exchange=Exchange.JPX,
            interval=Interval.MINUTE,
            start=start,
            end=end,
        )

        # Bars come sorted ascending, so within a session the first bar gives
        # the open and the last gives the close.
        ohlc: dict[str, tuple[float, float, float, float]] = {}
        for bar in bars:
            if bar.datetime.hour >= 17:
                session_dt = bar.datetime + timedelta(days=1)
            else:
                session_dt = bar.datetime
            date_key: str = session_dt.strftime("%Y-%m-%d")

            prev = ohlc.get(date_key)
            if prev is None:
                ohlc[date_key] = (
                    bar.open_price, bar.high_price, bar.low_price, bar.close_price
                )
            else:
                o, h, l, _c = prev
                ohlc[date_key] = (
                    o,
                    max(h, bar.high_price),
                    min(l, bar.low_price),
                    bar.close_price,
                )

        return ohlc

    def _load_futures_daily_close(
        self, month: str, days: int
    ) -> dict[str, float]:
        """Daily close per session date, derived from the daily OHLC."""
        return {
            d: ohlc[3]
            for d, ohlc in self._load_futures_daily_ohlc(month, days).items()
        }

    def run_analysis(self, *_args, show_warnings: bool = True) -> None:
        # *_args absorbs the bool emitted by QPushButton.clicked. show_warnings
        # is False for background auto-refresh so it never pops modal dialogs.
        # Stamp the refresh time (manual or auto) so updates are confirmable.
        self.last_update_label.setText(
            "最終更新: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        days: int = self.days_spin.value()

        all_bars: list[BarData] = _load_option_bars_with_today(days)
        if not all_bars:
            if show_warnings:
                QtWidgets.QMessageBox.warning(
                    self,
                    "データなし",
                    f"過去{days}日間のオプションデータが見つかりません",
                    QtWidgets.QMessageBox.Ok,
                )
            return

        _populate_month_combo(self.month_combo, all_bars)
        month: str = self.month_combo.currentText()
        futures_month: str = month if month != "全て" else ""
        interval_label: str = self.interval_combo.currentText()

        futures_ohlc: dict[str, tuple[float, float, float, float]] = {}
        pinned_series: dict[str, list[float]] = {}
        anchor_symbols: dict[str, str] = {}

        if interval_label == "1D":
            # Daily: per-strike option chain interpolation (current behaviour).
            bars: list[BarData] = _filter_bars_by_month(all_bars, month)
            date_labels, series = self.build_series(bars)
            if not date_labels:
                return
            pinned_series, anchor_symbols = self.build_strike_anchored_series(
                bars, date_labels
            )
            if futures_month:
                futures_ohlc = self._load_futures_daily_ohlc(futures_month, days)
        else:
            # Intraday: resample the futures minute-bar IV to the chosen interval.
            if not futures_month:
                if show_warnings:
                    QtWidgets.QMessageBox.warning(
                        self, "限月未選択",
                        "時間足(intraday)では限月を選択してください（「全て」不可）",
                        QtWidgets.QMessageBox.Ok,
                    )
                return
            hours: int = {"1H": 1, "2H": 2, "4H": 4, "8H": 8, "12H": 12}[interval_label]
            date_labels, series, futures_ohlc = self._build_intraday_series(
                futures_month, days, hours
            )
            if not date_labels:
                if show_warnings:
                    QtWidgets.QMessageBox.warning(
                        self, "データなし",
                        f"nk-{futures_month} の分足データが見つかりません",
                        QtWidgets.QMessageBox.Ok,
                    )
                return
            # Pinned (固定行使価格) from the recorded 15m per-strike option bars.
            pinned_series, anchor_symbols = self._build_intraday_pinned(
                futures_month, days, hours, date_labels
            )

        futures_prices: dict[str, float] = {d: v[3] for d, v in futures_ohlc.items()}

        delta_selection: str = self.delta_combo.currentText()
        ymin: float = self.ymin_spin.value()
        ymax: float = self.ymax_spin.value()
        self.update_chart(
            date_labels, series, delta_selection, ymin, ymax,
            futures_prices, pinned_series, anchor_symbols, futures_ohlc,
        )

    def update_chart(
        self,
        date_labels: list[str],
        series: dict[str, list[float]],
        delta_selection: str,
        ymin: float,
        ymax: float,
        futures_prices: dict[str, float] | None = None,
        pinned_series: dict[str, list[float]] | None = None,
        anchor_symbols: dict[str, str] | None = None,
        futures_ohlc: dict[str, tuple[float, float, float, float]] | None = None,
    ) -> None:
        self.fig.clear()
        if pinned_series is None:
            pinned_series = {}
        if anchor_symbols is None:
            anchor_symbols = {}

        x = np.arange(len(date_labels))

        if delta_selection == "全デルタ":
            plot_targets = list(self.DELTA_TARGETS)
        else:
            plot_targets = [t for t in self.DELTA_TARGETS if t[0] == delta_selection]

        # Display order (legend & lines): Put → ATM → Call (unknown labels last).
        _display_order = {"Put Δ0.10": 0, "ATM (Δ0.50)": 1, "Call Δ0.10": 2}
        plot_targets.sort(key=lambda t: _display_order.get(t[0], 99))

        show_decay: bool = self.decay_check.isChecked()
        show_envelope: bool = self.envelope_check.isChecked()
        show_skew: bool = self.skew_check.isChecked()
        show_pinned: bool = self.pinned_check.isChecked() and bool(pinned_series)
        envelope_window: int = 20

        atm_label: str = "ATM (Δ0.50)"
        skew_series: dict[str, list[float]] = {}
        if atm_label in series:
            atm_vals = series[atm_label]
            for label in series:
                if label == atm_label:
                    continue
                skew_series[label] = [
                    (v - a) if not (np.isnan(v) or np.isnan(a)) else float("nan")
                    for v, a in zip(series[label], atm_vals)
                ]

        skew_targets: list[tuple[str, float, str]] = [
            t for t in plot_targets if t[0] != atm_label and t[0] in skew_series
        ]
        has_skew_plot: bool = show_skew and bool(skew_targets)

        # Build a vertical stack of subplots based on which overlays are enabled.
        #   row 0: main IV chart (always present)
        #   row 1: skew subplot (optional)
        height_ratios: list[int] = [6]
        if has_skew_plot:
            height_ratios.append(2)

        ax_skew = None
        ax_skew_right = None
        if len(height_ratios) == 1:
            ax = self.fig.add_subplot(111)
        else:
            gs = self.fig.add_gridspec(
                len(height_ratios), 1, height_ratios=height_ratios, hspace=0.08
            )
            ax = self.fig.add_subplot(gs[0, 0])
            row_idx = 1
            if has_skew_plot:
                ax_skew = self.fig.add_subplot(gs[row_idx, 0], sharex=ax)
                row_idx += 1

        def _compute_theory(arr: np.ndarray) -> np.ndarray | None:
            """Return the 1/√t decay theory curve from the most-recent peak, or None."""
            valid_mask = ~np.isnan(arr)
            if valid_mask.sum() < 3:
                return None
            peak_idx = int(np.nanargmax(arr))
            peak_v = float(arr[peak_idx])
            base_v = float(np.nanmin(arr))
            spike = peak_v - base_v
            if spike <= 0:
                return None
            theory = np.full(len(arr), np.nan)
            for j in range(peak_idx, len(arr)):
                theory[j] = base_v + spike / np.sqrt(j - peak_idx + 1)
            return theory

        for label, target_delta, color in plot_targets:
            values: list[float] = series[label]

            # IV残像 (rolling high/low band) — plotted behind the line
            if show_envelope:
                arr = np.array(values, dtype=float)
                n = len(arr)
                roll_hi = np.full(n, np.nan)
                roll_lo = np.full(n, np.nan)
                for i in range(n):
                    j0 = max(0, i - envelope_window + 1)
                    window = arr[j0:i + 1]
                    valid = window[~np.isnan(window)]
                    if valid.size:
                        roll_hi[i] = valid.max()
                        roll_lo[i] = valid.min()
                mask = ~(np.isnan(roll_hi) | np.isnan(roll_lo))
                if mask.any():
                    ax.fill_between(
                        x, roll_lo, roll_hi, where=mask,
                        color=color, alpha=0.2, linewidth=0,
                    )

            ax.plot(x, values, color=color, linewidth=1.5, label=label, marker=".", markersize=3)

            # IV理論減衰 — from most-recent peak, decay ∝ 1/√(t+1)
            if show_decay:
                arr = np.array(values, dtype=float)
                theory = _compute_theory(arr)
                if theory is not None:
                    ax.plot(
                        x, theory,
                        color=color, linewidth=1.0, linestyle="--",
                        alpha=0.7, label=f"{label} 理論減衰",
                    )

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
                    bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=1),
                )

        # --- Strike-anchored (pinned) IV — dotted line on the main IV chart ---
        # Tracks one specific contract per Δ-target so its IV move is not
        # contaminated by the chain rolling along the smile when futures move.
        if show_pinned:
            # Distinct colours for the pinned lines so they don't blend with the
            # normal delta lines (ATM=white / Put=orange / Call=cyan).
            pinned_colors: dict[str, str] = {
                "ATM (Δ0.50)": "#e040fb",   # magenta
                "Put Δ0.10": "#c6ff00",     # lime
                "Call Δ0.10": "#ff6d00",    # deep orange
            }
            for label, target_delta, color in plot_targets:
                pinned_values = pinned_series.get(label)
                if not pinned_values:
                    continue
                arr = np.array(pinned_values, dtype=float)
                if not np.any(~np.isnan(arr)):
                    continue
                pin_color: str = pinned_colors.get(label, color)
                anchor_sym = anchor_symbols.get(label, "")
                # Pull strike out of "nk-2606-P-54000" → "54000" for the legend.
                strike_str: str = anchor_sym.split("-")[-1] if anchor_sym else ""
                pinned_label: str = f"{label} ピン留め(K={strike_str})" if strike_str else f"{label} ピン留め"
                ax.plot(
                    x, pinned_values,
                    color=pin_color, linewidth=1.6, linestyle="--",
                    alpha=0.95, label=pinned_label,
                    marker="x", markersize=4,
                )

                # 前日差（固定ストライクの日次IV差）をピン留め線上に表示。
                # デルタ線のラベルと重ならないよう点の下側 (va="top") に置く。
                fs_pin: int = 11 if delta_selection != "全デルタ" else 9
                for i in range(1, len(pinned_values)):
                    prev_v: float = pinned_values[i - 1]
                    curr_v: float = pinned_values[i]
                    if np.isnan(prev_v) or np.isnan(curr_v):
                        continue
                    ax.text(
                        (x[i - 1] + x[i]) / 2, (prev_v + curr_v) / 2,
                        f"{curr_v - prev_v:+.1f}",
                        ha="center", va="top", fontsize=fs_pin,
                        # Match the distinct pinned-line colour so the label is
                        # tied to the pinned line, not the normal strike line.
                        color=pin_color, fontweight="bold",
                        bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=1),
                    )

        ax.legend(loc="upper left", fontsize=8, framealpha=0.7)
        ax.grid(True, alpha=0.3)

        # --- Skew subplot: (IV at target Δ) − (IV at ATM) ---
        # Put (negative Δ) and Call (positive Δ) skews live on very different
        # scales (Put ≈ +10, Call ≈ -2), so when both are visible we put Put
        # on the left axis and Call on a twin right axis to remove the gap.
        if ax_skew is not None:
            has_put_skew: bool = any(td < 0 for _, td, _ in skew_targets)
            has_call_skew: bool = any(td > 0 for _, td, _ in skew_targets)
            use_twin_skew: bool = has_put_skew and has_call_skew
            if use_twin_skew:
                ax_skew_right = ax_skew.twinx()

            skew_legend_lines: list = []
            for label, target_delta, color in skew_targets:
                skew_values: list[float] = skew_series[label]
                target_ax = ax_skew_right if (use_twin_skew and target_delta > 0) else ax_skew
                line, = target_ax.plot(
                    x, skew_values,
                    color=color, linewidth=1.5,
                    label=f"{label} − ATM",
                    marker=".", markersize=3,
                )
                skew_legend_lines.append(line)

                # 前日比 on the skew line too — placed on the same axis as the line
                fs_skew: int = 9
                for i in range(1, len(skew_values)):
                    prev_v = skew_values[i - 1]
                    curr_v = skew_values[i]
                    if np.isnan(prev_v) or np.isnan(curr_v):
                        continue
                    diff_v = curr_v - prev_v
                    if abs(diff_v) < 0.05:
                        continue
                    mid_x_s = (x[i - 1] + x[i]) / 2
                    mid_y_s = (prev_v + curr_v) / 2
                    target_ax.text(
                        mid_x_s, mid_y_s, f"{diff_v:+.1f}",
                        ha="center", va="bottom", fontsize=fs_skew,
                        color=color, fontweight="bold",
                        bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=1),
                    )

            ax_skew.axhline(0, color="#ffffff", linewidth=0.6, alpha=0.5)
            ax_skew.legend(
                skew_legend_lines,
                [ln.get_label() for ln in skew_legend_lines],
                loc="upper left", fontsize=8, framealpha=0.7,
            )
            ax_skew.grid(True, alpha=0.3)
            if use_twin_skew:
                ax_skew.set_ylabel("Put − ATM", fontsize=9)
                ax_skew_right.set_ylabel("Call − ATM", fontsize=9)
                ax_skew_right.tick_params(axis="y", labelsize=8)
            else:
                ax_skew.set_ylabel("スキュー\n(IV − ATM)", fontsize=9)
            ax_skew.tick_params(axis="y", labelsize=8)

        # Tighten skew Y-axis to the actual IV−ATM data range (ignore theory
        # decay overlay so the dashed reference can't stretch the view).
        # When the twin axis is in play, each side gets its own tight bounds.
        def _tight_ylim(target_ax, vals: list[float]) -> None:
            if not vals:
                return
            lo: float = min(vals)
            hi: float = max(vals)
            pad: float = (hi - lo) * 0.08 if hi > lo else 0.5
            target_ax.set_ylim(lo - pad, hi + pad)

        if ax_skew is not None and skew_targets:
            if ax_skew_right is not None:
                left_vals: list[float] = [
                    v
                    for lbl, td, _ in skew_targets if td < 0
                    for v in skew_series.get(lbl, []) if not np.isnan(v)
                ]
                right_vals: list[float] = [
                    v
                    for lbl, td, _ in skew_targets if td > 0
                    for v in skew_series.get(lbl, []) if not np.isnan(v)
                ]
                _tight_ylim(ax_skew, left_vals)
                _tight_ylim(ax_skew_right, right_vals)
            else:
                all_vals: list[float] = [
                    v
                    for lbl, _, _ in skew_targets
                    for v in skew_series.get(lbl, []) if not np.isnan(v)
                ]
                _tight_ylim(ax_skew, all_vals)

        if ymin > 0 or ymax > 0:
            if ymin > 0 and ymax > 0 and ymax > ymin:
                ax.set_ylim(ymin, ymax)
            elif ymin > 0:
                ax.set_ylim(bottom=ymin)
            elif ymax > 0:
                ax.set_ylim(top=ymax)

        # Plot futures price on secondary Y-axis as daily OHLC candlesticks.
        futures_values: list[float] = []
        if futures_prices:
            for d in date_labels:
                price = futures_prices.get(d, float("nan"))
                futures_values.append(price)

        if futures_ohlc and any(d in futures_ohlc for d in date_labels):
            ax2 = ax.twinx()
            # Render the futures candlesticks BEHIND the IV lines: drop the twin
            # axis below the primary axis and make the primary background
            # transparent so its lines/labels stay readable on top.
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)
            up_color: str = "#ff4b4b"       # 陽線 (close >= open) — 株価チャット UP_COLOR
            down_color: str = "#4bffff"     # 陰線 (close < open) — 株価チャット DOWN_COLOR
            body_w: float = 0.6
            lows: list[float] = []
            highs: list[float] = []
            for xi, d in enumerate(date_labels):
                ov = futures_ohlc.get(d)
                if ov is None:
                    continue
                o, h, l, c = ov
                color: str = up_color if c >= o else down_color
                body_top: float = max(o, c)
                body_bottom: float = min(o, c)
                # Draw the wick only as the shadows OUTSIDE the body (upper and
                # lower), so it never overlaps / bleeds through the body.
                if h > body_top:
                    ax2.vlines(xi, body_top, h, color=color, linewidth=3.5, zorder=2, alpha=0.55)
                if body_bottom > l:
                    ax2.vlines(xi, l, body_bottom, color=color, linewidth=3.5, zorder=2, alpha=0.55)
                # Open-close body
                body_height: float = abs(c - o)
                if body_height <= 0:
                    # Doji: draw a thin sliver so the bar stays visible
                    body_height = max((h - l) * 0.02, 0.5)
                ax2.bar(
                    xi, body_height, bottom=body_bottom, width=body_w,
                    color=color, edgecolor=color, linewidth=0.5,
                    zorder=3, align="center", alpha=0.55,
                )
                lows.append(l)
                highs.append(h)
            if lows and highs:
                lo, hi = min(lows), max(highs)
                pad: float = (hi - lo) * 0.05 or 1.0
                ax2.set_ylim(lo - pad, hi + pad)
            ax2.set_ylabel("先物価格", color="#66ff66")
            ax2.tick_params(axis="y", labelcolor="#66ff66")
            ax2.legend(
                handles=[
                    Patch(facecolor=up_color, label="先物 陽線"),
                    Patch(facecolor=down_color, label="先物 陰線"),
                ],
                loc="upper right", fontsize=8, framealpha=0.7,
            )
        elif futures_prices and any(not np.isnan(v) for v in futures_values):
            # Fallback to a line when only close prices are available
            ax2 = ax.twinx()
            # Keep the futures line behind the IV lines (see note above).
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)
            ax2.plot(
                x, futures_values,
                color="#66ff66", linewidth=1.5, linestyle="-",
                label="先物", marker=".", markersize=3, alpha=0.8,
            )
            ax2.set_ylabel("先物価格", color="#66ff66")
            ax2.tick_params(axis="y", labelcolor="#66ff66")
            ax2.legend(loc="upper right", fontsize=8, framealpha=0.7)

        n_dates: int = len(date_labels)

        # Session-boundary vertical lines (intraday buckets only):
        #   night session start → 16:00 bucket (先物の取引日の起点、17:00立会開始)
        #   day session start   → 08:00 bucket (8:45立会開始)
        night_idx = [i for i, lbl in enumerate(date_labels) if lbl.endswith(" 16:00")]
        day_idx = [i for i, lbl in enumerate(date_labels) if lbl.endswith(" 08:00")]
        for target_ax in (ax, ax_skew):
            if target_ax is None:
                continue
            for i in night_idx:
                target_ax.axvline(x=i, color="#9aa0a6", linewidth=0.8,
                                  linestyle="-", alpha=0.45, zorder=0)
            for i in day_idx:
                target_ax.axvline(x=i, color="#5f6368", linewidth=0.6,
                                  linestyle="--", alpha=0.4, zorder=0)

        if night_idx:
            # Date labels at night-session boundaries (thin if too many).
            lstep: int = max(1, len(night_idx) // 15)
            sel = night_idx[::lstep]
            tick_positions = sel
            tick_labels: list[str] = [date_labels[i][5:] for i in sel]
        else:
            step: int = max(1, n_dates // 15)
            tick_positions = [i for i in range(0, n_dates, step)]
            tick_labels = [date_labels[i][5:] for i in range(0, n_dates, step)]

        title: str = f"IV時系列 — {delta_selection}" if delta_selection != "全デルタ" else "IV時系列（デルタレベル別）"
        ax.set_title(title, fontsize=12)
        ax.set_ylabel("IV (年率%)")

        # X-axis labels go on the bottom-most subplot only.
        if ax_skew is not None:
            bottom_ax = ax_skew
        else:
            bottom_ax = ax
        bottom_ax.set_xticks(tick_positions)
        bottom_ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)
        bottom_ax.set_xlabel("日付")
        for upper_ax in (ax, ax_skew):
            if upper_ax is None or upper_ax is bottom_ax:
                continue
            upper_ax.set_xticks(tick_positions)
            upper_ax.tick_params(axis="x", labelbottom=False)
            upper_ax.set_xlabel("")

        # Setup cursor
        self._cursor_ax = ax
        self._cursor_ax_skew = ax_skew
        self._cursor_date_labels = date_labels
        self._cursor_series = {label: series[label] for label, _, _ in plot_targets}
        self._cursor_skew_series = {
            label: skew_series[label]
            for label, _, _ in skew_targets
            if ax_skew is not None and label in skew_series
        }
        # Cursor label display order: Put → ATM → Call (unknown labels last).
        _cursor_order = {"Put Δ0.10": 0, "ATM (Δ0.50)": 1, "Call Δ0.10": 2}
        self._cursor_plot_targets = sorted(
            plot_targets, key=lambda t: _cursor_order.get(t[0], 99)
        )
        self._cursor_skew_targets = list(skew_targets) if ax_skew is not None else []
        self._cursor_pinned_series = (
            {label: pinned_series[label] for label in pinned_series}
            if show_pinned else {}
        )
        self._cursor_anchor_symbols = dict(anchor_symbols) if show_pinned else {}
        self._cursor_futures_values = futures_values

        self._cursor_vline = ax.axvline(x=0, color="#ffffff", linewidth=0.5, linestyle="--", alpha=0.5, visible=False)
        self._cursor_text = ax.text(
            0.02, 0.98, "",
            transform=ax.transAxes,
            fontsize=8,
            color="#ffffff",
            verticalalignment="top",
            bbox=dict(boxstyle="round,pad=0.3", fc="#333333", ec="#888888", alpha=0.9),
        )
        self._cursor_text.set_visible(False)

        # Skew subplot cursor mirror
        self._cursor_vline_skew = None
        if ax_skew is not None:
            self._cursor_vline_skew = ax_skew.axvline(
                x=0, color="#ffffff", linewidth=0.5, linestyle="--", alpha=0.5, visible=False,
            )

        if self._cursor_cid:
            self.canvas.mpl_disconnect(self._cursor_cid)
        self._cursor_cid = self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)

        # Trim outer whitespace to match IVEventDecayChart compactness
        bottom_margin = 0.07 if ax_skew is not None else 0.08
        self.fig.subplots_adjust(left=0.045, right=0.97, top=0.96, bottom=bottom_margin, hspace=0.05)
        self.canvas.draw()

    def _on_mouse_move(self, event) -> None:
        if event.inaxes is None or self._cursor_ax is None:
            if self._cursor_vline:
                self._cursor_vline.set_visible(False)
            if self._cursor_text:
                self._cursor_text.set_visible(False)
            if getattr(self, "_cursor_vline_skew", None):
                self._cursor_vline_skew.set_visible(False)
            self.canvas.draw_idle()
            return

        ix: int = int(round(event.xdata))
        if ix < 0 or ix >= len(self._cursor_date_labels):
            self._cursor_vline.set_visible(False)
            self._cursor_text.set_visible(False)
            if getattr(self, "_cursor_vline_skew", None):
                self._cursor_vline_skew.set_visible(False)
            self.canvas.draw_idle()
            return

        self._cursor_vline.set_xdata([ix])
        self._cursor_vline.set_visible(True)
        if getattr(self, "_cursor_vline_skew", None):
            self._cursor_vline_skew.set_xdata([ix])
            self._cursor_vline_skew.set_visible(True)

        date_str: str = self._cursor_date_labels[ix]
        lines: list[str] = [date_str]

        for label, _delta, color in self._cursor_plot_targets:
            values = self._cursor_series.get(label, [])
            if ix < len(values) and not np.isnan(values[ix]):
                short_label = label.split(" ")[0]
                lines.append(f"{short_label}: {values[ix]:.1f}%")

        # Pinned (strike-anchored) IV values
        if getattr(self, "_cursor_pinned_series", None):
            anchor_syms = getattr(self, "_cursor_anchor_symbols", {})
            for label, _delta, color in self._cursor_plot_targets:
                pinned_vals = self._cursor_pinned_series.get(label, [])
                if ix < len(pinned_vals) and not np.isnan(pinned_vals[ix]):
                    short_label = label.split(" ")[0]
                    sym = anchor_syms.get(label, "")
                    strike_part = sym.split("-")[-1] if sym else ""
                    suffix = f"@K{strike_part}" if strike_part else ""
                    lines.append(f"{short_label}{suffix}: {pinned_vals[ix]:.1f}%")

        # Skew values when the skew subplot is enabled
        if getattr(self, "_cursor_skew_series", None):
            for label, _delta, color in getattr(self, "_cursor_skew_targets", []):
                skew_vals = self._cursor_skew_series.get(label, [])
                if ix < len(skew_vals) and not np.isnan(skew_vals[ix]):
                    short_label = label.split(" ")[0]
                    lines.append(f"{short_label}−ATM: {skew_vals[ix]:+.1f}%")

        if self._cursor_futures_values and ix < len(self._cursor_futures_values):
            fv = self._cursor_futures_values[ix]
            if not np.isnan(fv):
                lines.append(f"先物: {fv:.0f}")

        # Convert mouse pixel position to axes fraction to avoid twinx y-coordinate issue
        ax_frac = self._cursor_ax.transAxes.inverted().transform((event.x, event.y))
        self._cursor_text.set_position((ax_frac[0] + 0.02, ax_frac[1] + 0.02))
        self._cursor_text.set_text("\n".join(lines))
        self._cursor_text.set_visible(True)

        self.canvas.draw_idle()


class PayoffDiagramChart(QtWidgets.QWidget):
    """ペイオフ図 - Position payoff diagram with IV adjustment."""

    SETTING_FILENAME: str = "payoff_diagram_setting.json"

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name

        # Simulation positions: list of dicts with live OptionData/UnderlyingData refs
        self.sim_positions: list[dict] = []

        # RSS 約定取込: persistent execution ledger. RSS 約定一覧 is current-day
        # only, so we accumulate every unique fill seen across days/clicks here
        # (deduped by signature) and FIFO-reconstruct positions from the whole
        # ledger — this keeps prior-day opens and lets today's 転売 close them.
        # Each entry: {date, code, name, trade, qty, price, fee, tax}.
        self._rss_ledger: list[dict] = []

        # Real-time update timer
        self._update_timer: QtCore.QTimer = QtCore.QTimer()
        self._update_timer.setInterval(1000)
        self._update_timer.timeout.connect(self._on_update_timer)
        self._pending_update: bool = False

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

        # Contract month selector
        self.sim_month_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.sim_month_combo.setFixedWidth(120)
        self.sim_month_combo.currentIndexChanged.connect(self._on_month_changed)

        # Call/Put selector
        self.sim_cp_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.sim_cp_combo.addItems(["コール", "プット"])
        self.sim_cp_combo.currentIndexChanged.connect(self._on_cp_changed)

        # Strike price selector (populated dynamically)
        self.sim_strike_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.sim_strike_combo.setFixedWidth(100)

        # Futures type selector (for mini/large)
        self.sim_futures_type_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.sim_futures_type_combo.addItems(["先物ミニ", "先物ラージ"])

        # Lots spinbox for OP追加
        self.sim_lots_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.sim_lots_spin.setRange(-999, 999)
        self.sim_lots_spin.setValue(5)

        # Lots spinbox for 先物追加
        self.sim_futures_lots_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.sim_futures_lots_spin.setRange(-999, 999)
        self.sim_futures_lots_spin.setValue(-5)

        # Total P&L label (unrealized: open + enabled)
        self.sim_total_pnl_label: QtWidgets.QLabel = QtWidgets.QLabel("合計損益: ---")
        self.sim_total_pnl_label.setStyleSheet("font-weight: bold; font-size: 13px;")

        # Realized P&L label (closed + enabled)
        self.sim_realized_pnl_label: QtWidgets.QLabel = QtWidgets.QLabel("実現損益: ---")
        self.sim_realized_pnl_label.setStyleSheet("font-weight: bold; font-size: 13px;")

        # Simulation position table (read-only display)
        sim_table_headers: list[str] = [
            "有効", "限月", "種類", "行使価格", "枚数", "建時刻",
            "建値", "現在値", "建IV%", "現在IV%", "IV差分", "先物差", "合計損益", "損益",
            "Δ寄与", "Γ寄与", "Θ寄与", "V寄与",
            "建Δ", "建Γ", "建Θ", "建V",
            "現Δ", "現Γ", "現Θ", "現V",
            "決済済", "決済値", "手数料", "取引手数料(税込)",
        ]
        self.sim_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget(0, len(sim_table_headers))
        self.sim_table.setHorizontalHeaderLabels(sim_table_headers)
        # Keep columns at their content width (don't stretch the last one to
        # fill) so the content can overflow horizontally, and always show both
        # scrollbars — needed for touch scrolling via Remote Desktop, like the
        # 収益 (result) table.
        self.sim_table.horizontalHeader().setStretchLastSection(False)
        self.sim_table.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self.sim_table.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        # Reserve height so the positions table actually shows taller. A bare
        # setMaximumHeight has no effect here — the result_table below has
        # stretch=1 and absorbs all the extra vertical space, so the table
        # stays at its small content-based size hint. setMinimumHeight forces
        # the layout to give it the space. (600 = triple the original 200.)
        self.sim_table.setMinimumHeight(675)
        # Reduce cell padding for a tighter layout
        self.sim_table.setStyleSheet(
            "QTableWidget::item { padding: 0px 2px; }"
            "QHeaderView::section { padding: 1px 3px; }"
        )
        self.sim_table.verticalHeader().setDefaultSectionSize(18)
        self.sim_table.horizontalHeader().setDefaultSectionSize(55)
        self.sim_table.horizontalHeader().setMinimumSectionSize(30)
        self.sim_table.itemChanged.connect(self._on_sim_table_item_changed)

        add_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("OP追加")
        add_btn.clicked.connect(self._add_sim_row)
        add_futures_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("先物追加")
        add_futures_btn.clicked.connect(self._add_sim_futures_row)
        del_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("行削除")
        del_btn.clicked.connect(self._remove_sim_row)
        rss_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("約定取込(RSS)")
        rss_btn.setToolTip(
            "マーケットスピードII RSS の先物OP約定一覧から建玉を取り込みます。\n"
            "Excel と マーケットスピードII を起動・ログインしておいてください。"
        )
        rss_btn.clicked.connect(self._import_rss_executions)
        rss_clear_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("RSS履歴クリア")
        rss_clear_btn.setToolTip(
            "RSS約定の累計履歴（建玉ledger）を消去し、RSS取込で作成した行を\n"
            "すべて削除します。新しい期間を始めるときに使用します（手動追加行は残ります）。"
        )
        rss_clear_btn.clicked.connect(self._clear_rss_history)

        sim_param_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        sim_param_hbox.addWidget(QtWidgets.QLabel("限月"))
        sim_param_hbox.addWidget(self.sim_month_combo)
        sim_param_hbox.addWidget(QtWidgets.QLabel("種類"))
        sim_param_hbox.addWidget(self.sim_cp_combo)
        sim_param_hbox.addWidget(QtWidgets.QLabel("行使価格"))
        sim_param_hbox.addWidget(self.sim_strike_combo)
        sim_param_hbox.addWidget(QtWidgets.QLabel("枚数"))
        sim_param_hbox.addWidget(self.sim_lots_spin)
        sim_param_hbox.addWidget(self.sim_futures_type_combo)
        sim_param_hbox.addWidget(QtWidgets.QLabel("枚数"))
        sim_param_hbox.addWidget(self.sim_futures_lots_spin)
        # RSS履歴クリア on the far left, separated from the frequently-used
        # buttons by the stretch, to avoid an accidental click.
        sim_param_hbox.addWidget(rss_clear_btn)
        sim_param_hbox.addStretch()
        sim_param_hbox.addWidget(add_btn)
        sim_param_hbox.addWidget(add_futures_btn)
        sim_param_hbox.addWidget(del_btn)
        sim_param_hbox.addWidget(rss_btn)

        sim_pnl_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        sim_pnl_hbox.addWidget(self.sim_total_pnl_label)
        sim_pnl_hbox.addWidget(self.sim_realized_pnl_label)
        sim_pnl_hbox.addStretch()

        sim_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        sim_layout.addLayout(sim_param_hbox)
        sim_layout.addLayout(sim_pnl_hbox)
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
        self.step_combo.addItems(["0.5σ", "1.0σ"])
        # Re-run analysis when the price scope / step changes (auto-refresh).
        self.price_range_spin.valueChanged.connect(self._on_scope_changed)
        self.step_combo.currentIndexChanged.connect(self._on_scope_changed)

        self.days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.days_spin.setSuffix(" 日")
        self.days_spin.setMinimum(0)
        self.days_spin.setMaximum(30)
        self.days_spin.setValue(1)

        self.iv_sensitivity_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.iv_sensitivity_spin.setSuffix(" %/0.5σ")
        self.iv_sensitivity_spin.setMinimum(-20.0)
        self.iv_sensitivity_spin.setMaximum(20.0)
        self.iv_sensitivity_spin.setSingleStep(0.1)
        self.iv_sensitivity_spin.setValue(-1.0)
        self.iv_sensitivity_spin.setDecimals(2)

        # Auto-estimate IV感応度 from historical spot-vol regression
        self.iv_auto_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("自動")
        self.iv_auto_btn.setToolTip(
            "過去の ATM IV 日次変化と先物日次変化の回帰から\n"
            "IV感応度 (%IV / +0.5σ) を自動推定します"
        )
        self.iv_auto_btn.clicked.connect(self._auto_iv_sensitivity)

        # Reset IV感応度 to the ATM IV変動値 per +0.5σ (recent 参照日数).
        self.iv_reset_btn: QtWidgets.QPushButton = QtWidgets.QPushButton("ATM既定")
        self.iv_reset_btn.setToolTip(
            "IV感応度を「0.5σ ATM IV変動値 / +0.5σ」に設定します。\n"
            "= 0.5 × 日次ATM IV(%) = 株価チャットのATM 0.5σバンド（≈0.9%/+0.5σ）"
        )
        self.iv_reset_btn.clicked.connect(self._reset_iv_sensitivity)

        # 自動計算の参照日数 と デルタ種別（ポジションに合わせる）
        self.iv_auto_days_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.iv_auto_days_spin.setSuffix(" 日")
        self.iv_auto_days_spin.setMinimum(1)
        self.iv_auto_days_spin.setMaximum(90)
        self.iv_auto_days_spin.setValue(3)
        self.iv_auto_days_spin.setToolTip("自動計算で参照する直近日数")

        self.iv_auto_delta_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.iv_auto_delta_combo.addItems(
            ["Put Δ0.10", "ATM (Δ0.50)", "Call Δ0.10", "全デルタ"]
        )
        self.iv_auto_delta_combo.setToolTip(
            "自動計算で使うIVのデルタ種別（ポジションに合わせて選択）"
        )

        self.show_legs_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("個別ポジション")
        self.show_legs_check.setChecked(True)

        self.show_expiry_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("満期線")
        self.show_expiry_check.setChecked(True)

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
        ctrl_grid.addWidget(self.iv_auto_btn, 1, 4)
        ctrl_grid.addWidget(QtWidgets.QLabel("参照日数"), 1, 5)
        ctrl_grid.addWidget(self.iv_auto_days_spin, 1, 6)
        ctrl_grid.addWidget(QtWidgets.QLabel("Δ種別"), 1, 7)
        ctrl_grid.addWidget(self.iv_auto_delta_combo, 1, 8)
        ctrl_grid.addWidget(self.iv_reset_btn, 1, 9)

        btn_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        btn_hbox.addWidget(self.show_legs_check)
        btn_hbox.addWidget(self.show_expiry_check)
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
        self.splitter: QtWidgets.QSplitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(self.chart_tabs)
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 6)

        main_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        main_layout.addWidget(self.splitter)
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
        sim_positions_data: list[dict] = []
        for pos in self.sim_positions:
            sim_positions_data.append({
                "vt_symbol": pos.get("vt_symbol", ""),
                "chain_symbol": pos["chain_symbol"],
                "kind": pos["kind"],
                "cp": pos["cp"],
                "strike": pos["strike"],
                "lots": pos["lots"],
                "entry_price": pos["entry_price"],
                "entry_iv": pos["entry_iv"],
                "entry_underlying": pos.get("entry_underlying", 0),
                "entry_tte": pos.get("entry_tte", 0),
                "entry_time": pos.get("entry_time", ""),
                "entry_delta": pos.get("entry_delta", 0),
                "entry_gamma": pos.get("entry_gamma", 0),
                "entry_theta": pos.get("entry_theta", 0),
                "entry_vega": pos.get("entry_vega", 0),
                "size": pos["size"],
                "futures_multiplier": pos.get("futures_multiplier", 1.0),
                "enabled": pos.get("enabled", True),
                "closed": pos.get("closed", False),
                "close_price": pos.get("close_price", 0.0),
                "manual_price": pos.get("manual_price", 0.0),
                "label": pos["label"],
                "fee": pos.get("fee", 0.0),
                "rss_imported": pos.get("rss_imported", False),
                "rss_batch": pos.get("rss_batch", 0),
            })

        data: dict = {
            "mode": self.mode_combo.currentIndex(),
            "sim_positions": sim_positions_data,
            "price_range": self.price_range_spin.value(),
            "step": self.step_combo.currentText(),
            "days": self.days_spin.value(),
            "iv_sensitivity": self.iv_sensitivity_spin.value(),
            "iv_auto_days": self.iv_auto_days_spin.value(),
            "iv_auto_delta": self.iv_auto_delta_combo.currentText(),
            "show_legs": self.show_legs_check.isChecked(),
            "show_expiry": self.show_expiry_check.isChecked(),
            "window_width": self.width(),
            "window_height": self.height(),
            "splitter_sizes": self.splitter.sizes(),
            "rss_ledger": self._rss_ledger,
        }
        save_json(self.SETTING_FILENAME, data)

    def _load_settings(self) -> None:
        """Restore settings and simulation positions from JSON."""
        data: dict = load_json(self.SETTING_FILENAME)
        if not data:
            return

        self.mode_combo.setCurrentIndex(data.get("mode", 0))
        self.price_range_spin.setValue(data.get("price_range", 5000))
        self.days_spin.setValue(data.get("days", 1))
        self.iv_sensitivity_spin.setValue(data.get("iv_sensitivity", -1.0))
        self.iv_auto_days_spin.setValue(data.get("iv_auto_days", 3))
        _auto_delta = data.get("iv_auto_delta", "")
        _adi = self.iv_auto_delta_combo.findText(_auto_delta)
        if _adi >= 0:
            self.iv_auto_delta_combo.setCurrentIndex(_adi)
        self.show_legs_check.setChecked(data.get("show_legs", True))
        self.show_expiry_check.setChecked(data.get("show_expiry", True))

        # Restore RSS execution ledger
        self._rss_ledger = list(data.get("rss_ledger", []))

        # Restore window size
        win_w: int = data.get("window_width", 0)
        win_h: int = data.get("window_height", 0)
        if win_w > 0 and win_h > 0:
            self.resize(win_w, win_h)

        # Restore splitter sizes
        splitter_sizes = data.get("splitter_sizes", [])
        if splitter_sizes and len(splitter_sizes) == self.splitter.count():
            self.splitter.setSizes([int(s) for s in splitter_sizes])

        step_text: str = data.get("step", "0.5σ")
        idx: int = self.step_combo.findText(step_text)
        if idx >= 0:
            self.step_combo.setCurrentIndex(idx)

        # Restore simulation positions (reconnect to live data)
        for pos_data in data.get("sim_positions", []):
            # Skip old-format entries that lack required keys
            if "kind" not in pos_data:
                continue

            vt_symbol: str = pos_data.get("vt_symbol", "")
            instrument = self.option_engine.get_instrument(vt_symbol) if vt_symbol else None

            opt_data = None
            und_data = None
            if pos_data["kind"] == "futures":
                if isinstance(instrument, UnderlyingData):
                    und_data = instrument
            else:
                if isinstance(instrument, OptionData):
                    opt_data = instrument

            self.sim_positions.append({
                "option_data": opt_data,
                "underlying_data": und_data,
                "chain_symbol": pos_data["chain_symbol"],
                "kind": pos_data["kind"],
                "cp": pos_data["cp"],
                "strike": pos_data["strike"],
                "lots": pos_data["lots"],
                "entry_price": pos_data["entry_price"],
                "entry_iv": pos_data["entry_iv"],
                "entry_underlying": pos_data.get("entry_underlying", 0),
                "entry_tte": pos_data.get("entry_tte", 0),
                "entry_time": pos_data.get("entry_time", ""),
                "entry_delta": pos_data.get("entry_delta", 0),
                "entry_gamma": pos_data.get("entry_gamma", 0),
                "entry_theta": pos_data.get("entry_theta", 0),
                "entry_vega": pos_data.get("entry_vega", 0),
                "size": pos_data["size"],
                "futures_multiplier": pos_data.get("futures_multiplier", 1.0),
                "enabled": pos_data.get("enabled", True),
                "closed": pos_data.get("closed", False),
                "close_price": pos_data.get("close_price", 0.0),
                "manual_price": pos_data.get("manual_price", 0.0),
                "label": pos_data["label"],
                "vt_symbol": vt_symbol,
                "fee": pos_data.get("fee", 0.0),
                "rss_imported": pos_data.get("rss_imported", False),
                "rss_batch": pos_data.get("rss_batch", 0),
            })

        if self.sim_positions:
            self._refresh_sim_table()
        self._toggle_mode()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Auto-save settings and stop timer when the window is closed."""
        self._update_timer.stop()
        self._save_settings()
        super().closeEvent(event)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """Restart the auto-refresh timer and refresh tables when reopened."""
        super().showEvent(event)
        if self.mode_combo.currentIndex() == 1:
            self._check_rss_on_open()
            if self.sim_positions:
                self._refresh_sim_table()
                self._run_sim_analysis()
            if not self._update_timer.isActive():
                self._update_timer.start()

    # ------------------------------------------------------------------
    #  Simulation panel helpers
    # ------------------------------------------------------------------
    def _toggle_mode(self) -> None:
        is_sim: bool = self.mode_combo.currentIndex() == 1
        self.sim_group.setVisible(is_sim)
        if is_sim:
            self._populate_months()
            self._update_timer.start()
        else:
            self._update_timer.stop()

    def _populate_months(self) -> None:
        """Populate contract month combo from available chains."""
        self.sim_month_combo.blockSignals(True)
        current: str = self.sim_month_combo.currentData() or ""
        self.sim_month_combo.clear()
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        for cs in sorted(portfolio.chains.keys()):
            self.sim_month_combo.addItem(cs.split(".")[0], cs)
        if current:
            idx = self.sim_month_combo.findData(current)
            if idx >= 0:
                self.sim_month_combo.setCurrentIndex(idx)
        self.sim_month_combo.blockSignals(False)
        self._populate_strikes()

    def _on_month_changed(self) -> None:
        self._populate_strikes()

    def _on_cp_changed(self) -> None:
        self._populate_strikes()

    def _populate_strikes(self) -> None:
        """Fill strike combo from the selected chain."""
        self.sim_strike_combo.clear()
        chain_symbol: str = self.sim_month_combo.currentData()
        if not chain_symbol:
            return
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        chain: ChainData | None = portfolio.chains.get(chain_symbol)
        if not chain:
            return
        is_call: bool = self.sim_cp_combo.currentText() == "コール"
        options_dict = chain.calls if is_call else chain.puts
        for index in chain.indexes:
            option = options_dict.get(index)
            if option:
                self.sim_strike_combo.addItem(str(int(option.strike_price)), index)

    def _add_sim_row(self) -> None:
        """Add an option position from live data."""
        chain_symbol: str = self.sim_month_combo.currentData()
        if not chain_symbol:
            return
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        chain: ChainData | None = portfolio.chains.get(chain_symbol)
        if not chain:
            return
        index: str = self.sim_strike_combo.currentData()
        if not index:
            return
        is_call: bool = self.sim_cp_combo.currentText() == "コール"
        options_dict = chain.calls if is_call else chain.puts
        option: OptionData | None = options_dict.get(index)
        if not option:
            return
        lots: int = self.sim_lots_spin.value()
        if lots == 0:
            return
        cp_str: str = "C" if option.option_type > 0 else "P"
        entry_underlying: float = option.underlying.mid_price if option.underlying else 0
        self.sim_positions.append({
            "option_data": option,
            "underlying_data": None,
            "chain_symbol": chain_symbol,
            "kind": "option",
            "cp": option.option_type,
            "strike": option.strike_price,
            "lots": lots,
            "entry_price": option.mid_price,
            "entry_iv": option.mid_impv,
            "entry_underlying": entry_underlying,
            "entry_tte": option.time_to_expiry,
            "entry_time": datetime.now().isoformat(),
            "entry_delta": option.theo_delta,
            "entry_gamma": option.theo_gamma,
            "entry_theta": option.theo_theta,
            "entry_vega": option.theo_vega,
            "size": option.size,
            "label": f"{cp_str}{option.strike_price:.0f}",
            "vt_symbol": option.vt_symbol,
            "enabled": True,
            "closed": False,
            "close_price": 0.0,
        })
        self._refresh_sim_table()

    def _add_sim_futures_row(self) -> None:
        """Add a futures position from the chain's underlying."""
        chain_symbol: str = self.sim_month_combo.currentData()
        if not chain_symbol:
            return
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        chain: ChainData | None = portfolio.chains.get(chain_symbol)
        if not chain or not chain.underlying:
            return
        underlying: UnderlyingData = chain.underlying
        lots: int = self.sim_futures_lots_spin.value()
        if lots == 0:
            return

        futures_type: str = self.sim_futures_type_combo.currentText()
        if futures_type == "先物ミニ":
            futures_multiplier: float = 0.1
        else:
            futures_multiplier = 1.0

        self.sim_positions.append({
            "option_data": None,
            "underlying_data": underlying,
            "chain_symbol": chain_symbol,
            "kind": "futures",
            "cp": 0,
            "strike": 0,
            "lots": lots,
            "entry_price": underlying.mid_price,
            "entry_iv": 0,
            "entry_underlying": underlying.mid_price,
            "entry_tte": 0,
            "entry_time": datetime.now().isoformat(),
            "entry_delta": underlying.size * futures_multiplier,
            "entry_gamma": 0,
            "entry_theta": 0,
            "entry_vega": 0,
            "size": underlying.size,
            "futures_multiplier": futures_multiplier,
            "label": f"{futures_type} {chain_symbol.split('.')[0]}",
            "vt_symbol": underlying.vt_symbol,
            "enabled": True,
            "closed": False,
            "close_price": 0.0,
        })
        self._refresh_sim_table()

    def _rss_file_path(self) -> str:
        """Path to rss_fop.xlsx inside the vnpy_optionmaster package."""
        pkg_dir: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(pkg_dir, "rss_fop.xlsx")

    def _check_rss_on_open(self) -> None:
        """On ペイオフ図 open: check that rss_fop.xlsx is open in the user's Excel.
        If it isn't open, create it (when it doesn't exist yet) or prompt the
        user to open it. The user must open it so the RSS add-in is loaded."""
        if getattr(self, "_rss_checked", False):
            return
        self._rss_checked = True

        try:
            from . import rss_import
        except Exception:
            return
        path: str = self._rss_file_path()

        try:
            _running, is_open = rss_import.workbook_status(path)
        except Exception as e:                       # pragma: no cover
            print(f"[RSS] status check failed: {e}")
            return

        if is_open:
            return                                    # already open — nothing to do

        if not os.path.exists(path):
            # Never created yet → auto-create with the formula.
            try:
                how = rss_import.create_rss_file(path)
            except Exception as e:
                QtWidgets.QMessageBox.warning(
                    self, "RSSファイル作成失敗",
                    f"rss_fop.xlsx を作成できませんでした:\n{e}",
                    QtWidgets.QMessageBox.Ok,
                )
                return
            if how == "excel":
                QtWidgets.QMessageBox.information(
                    self, "RSSファイル作成",
                    "rss_fop.xlsx を作成し、Excelで開きました。\n"
                    "マーケットスピードII にログインすると建玉が取り込めます。\n\n"
                    f"{path}",
                    QtWidgets.QMessageBox.Ok,
                )
            else:
                QtWidgets.QMessageBox.information(
                    self, "RSSファイル作成",
                    "rss_fop.xlsx を作成しました。\n"
                    "マーケットスピードII とExcelを起動し、次のファイルを開いてください:\n\n"
                    f"{path}",
                    QtWidgets.QMessageBox.Ok,
                )
        else:
            # Exists but not open → ask the user to open it.
            QtWidgets.QMessageBox.information(
                self, "RSSファイル未オープン",
                "先物OP約定一覧を取り込むには rss_fop.xlsx を開いてください。\n"
                "マーケットスピードII とExcelを起動し、次のファイルを開いてください:\n\n"
                f"{path}",
                QtWidgets.QMessageBox.Ok,
            )

    def _find_chain_by_yymm(self, portfolio: "PortfolioData", yymm: str) -> str | None:
        """Find the chain_symbol (e.g. 'nk-2609.JPX') whose month == yymm."""
        for cs in portfolio.chains.keys():
            m = re.search(r"(\d{4})", cs)
            if m and m.group(1) == yymm:
                return cs
        return None

    @staticmethod
    def _snap_price(price: float, kind: str, is_mini: bool = True) -> float:
        """Snap a live mid price DOWN to the exchange tick grid (呼値).
          オプション: 価格>=300 は刻み5、<300 は刻み1
          先物:       日経225ミニ 刻み5 / 日経225先物(ラージ) 刻み10
        e.g. option 302.5→300, futures(mini) 67478→67475.
        """
        if not price or price <= 0:
            return price
        if kind == "futures":
            tick: float = 5.0 if is_mini else 10.0
        else:
            tick = 5.0 if price >= 300 else 1.0
        return math.floor(price / tick) * tick

    def _step_sigma(self) -> float:
        """Parse the ステップ combo ('0.5σ' / '1.0σ') → float σ increment."""
        txt = self.step_combo.currentText().replace("σ", "").strip()
        try:
            return float(txt)
        except ValueError:
            return 0.5

    def _on_scope_changed(self, *_args) -> None:
        """Re-run analysis when 価格範囲 / ステップ changes (auto-refresh)."""
        if self.mode_combo.currentIndex() == 1:
            if self.sim_positions:
                self.run_analysis()
        else:
            self.run_analysis()

    @staticmethod
    def _build_price_levels(
        ref_price: float, base: float | None, daily_iv: float | None,
        price_range: float, step_sigma: float,
    ) -> list[tuple[float, str]]:
        """Descending (price, label) rows for the result table.

        Price levels sit on a σ grid anchored at 前日終値 (base):
        price = base × (1 + dailyIV × σ), σ stepped by step_sigma, kept within
        [ref_price ± price_range]. Always includes 前日終値(±0) and 現在.
        Falls back to a fixed 500pt descending grid when base/dailyIV missing.
        """
        lo: float = ref_price - price_range
        hi: float = ref_price + price_range
        levels: list[tuple[float, str]] = []
        if base and daily_iv:
            lo_sig: float = (lo / base - 1.0) / daily_iv
            hi_sig: float = (hi / base - 1.0) / daily_iv
            k_lo: int = int(math.ceil(lo_sig / step_sigma))
            k_hi: int = int(math.floor(hi_sig / step_sigma))
            seen_zero: bool = False
            for k in range(k_lo, k_hi + 1):
                sig: float = k * step_sigma
                p: float = base * (1.0 + daily_iv * sig)
                if abs(sig) < 1e-9:
                    levels.append((p, "±0(前終)"))
                    seen_zero = True
                else:
                    levels.append((p, f"{sig:+.1f}σ"))
            if not seen_zero and lo <= base <= hi:
                levels.append((base, "±0(前終)"))
            if lo <= ref_price <= hi:
                levels.append((ref_price, "現在"))
            levels.sort(key=lambda t: t[0], reverse=True)
        else:
            p = hi
            while p >= lo - 1e-9:
                levels.append((float(p), ""))
                p -= 500.0
        return levels

    @staticmethod
    def _estimate_trade_fee_yen(pos: dict, price: float) -> float:
        """楽天証券の取引手数料(税込, 円) 見積り。未決済ポジションの決済手数料の
        概算に使う。https://www.rakuten-sec.co.jp/web/fop/futures/commission/
          日経225先物:   275円/枚
          日経225ミニ:   38円/枚 (38.5円 1円未満切捨)
          日経225オプション: 売買代金×0.198% 最低198円 (1円未満切捨, 1取引あたり)
        """
        lots: int = abs(int(round(pos.get("lots", 0))))
        if lots == 0:
            return 0.0
        if pos.get("kind") == "futures":
            fm: float = pos.get("futures_multiplier", 1.0)
            per: float = 38.0 if fm < 1.0 else 275.0     # ミニ / ラージ
            return per * lots
        # 日経225オプション: 売買代金(円) = 単価(pt) × 枚数 × 1000
        notional: float = price * lots * 1000.0
        return max(198.0, float(math.floor(notional * 0.00198)))

    @staticmethod
    def _find_option_by_strike(chain: "ChainData", cp: str, strike: int) -> "OptionData | None":
        options_dict = chain.calls if cp == "C" else chain.puts
        for option in options_dict.values():
            if int(round(option.strike_price)) == int(strike):
                return option
        return None

    def _import_rss_executions(self) -> None:
        """Import 先物OP約定一覧 from マーケットスピードII RSS and rebuild positions.

        All fills (opens + closes) are read and matched per contract by FIFO
        (先入先出): 買建/売建 open lots; 転売 closes longs, 買戻 closes shorts,
        oldest lot first. Each closed match becomes a 決済み row (→ 実現損益);
        remaining open lots become open rows (→ 合計損益). 手数料+税金 of both
        the open and the close go into the 手数料 column. Previously RSS-imported
        rows are replaced each click; manually-added rows are kept.
        """
        try:
            from .rss_import import read_fop_executions, parse_instrument
        except Exception as e:                       # pragma: no cover
            QtWidgets.QMessageBox.warning(
                self, "RSS取込エラー", f"RSSモジュールの読み込みに失敗しました:\n{e}",
                QtWidgets.QMessageBox.Ok,
            )
            return

        try:
            executions = read_fop_executions()
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "RSS取込エラー", str(e), QtWidgets.QMessageBox.Ok,
            )
            return

        if not executions:
            QtWidgets.QMessageBox.information(
                self, "RSS取込",
                "先物OP約定一覧にデータが見つかりませんでした。\n"
                "（マーケットスピードIIにログイン済みで、当日約定があるか確認してください）",
                QtWidgets.QMessageBox.Ok,
            )
            return

        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        # --- Merge current fills into the persistent ledger ----------------
        # RSS 約定一覧 is current-day only, so accumulate every unique fill seen
        # across days/clicks and never remove prior-day fills. Dedup re-reads by
        # a signature whose date is NORMALISED (parsed → ISO) so a re-read with a
        # different date-string format does not look like a new fill.
        def _exec_dt(d: dict) -> datetime:
            s = str(d.get("date", "")).strip()
            for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return datetime.min

        def _sig(d: dict) -> str:
            dt = _exec_dt(d)
            dkey = dt.isoformat() if dt != datetime.min else str(d.get("date", "")).strip()
            return (f"{dkey}|{d['code']}|{d['trade']}|"
                    f"{d['qty']:.4f}|{d['price']:.4f}|{d['fee']:.2f}|{d['tax']:.2f}")

        ledger_counts = Counter(_sig(e) for e in self._rss_ledger)
        added_new: int = 0
        for ex in executions:
            d = {"date": ex.date, "code": ex.code, "name": ex.name,
                 "trade": ex.trade, "qty": ex.qty, "price": ex.price,
                 "fee": ex.fee, "tax": ex.tax}
            sig = _sig(d)
            if ledger_counts.get(sig, 0) > 0:
                ledger_counts[sig] -= 1               # already in the ledger
            else:
                self._rss_ledger.append(d)            # new fill → keep forever
                added_new += 1

        # Sort the whole ledger chronologically for FIFO matching.
        indexed = list(enumerate(self._rss_ledger))
        indexed.sort(key=lambda p: (_exec_dt(p[1]), p[0]))

        # Group parsed fills by contract (kind/month/CP/strike/mini).
        groups: dict[tuple, list] = {}
        unparsed: list[str] = []
        for _, d in indexed:
            info = parse_instrument(d["name"], d["code"])
            if info is None:
                unparsed.append(d["name"])
                continue
            ckey = (info["kind"], info["yymm"], info["cp"], info["strike"], info["is_mini"])
            groups.setdefault(ckey, []).append((d, info))

        # FIFO match per contract → position specs (open + closed lots).
        specs: list[dict] = []
        leftover: list[str] = []

        def _fifo_close(queue, q: float, price: float, fpu: float,
                        side_sign: int, info0: dict) -> float:
            """Close q units FIFO from queue, appending 決済み specs; return leftover."""
            while q > 0 and queue:
                lot = queue[0]                        # [qty, price, fee_per_unit, entry_dt]
                m = min(lot[0], q)
                specs.append({
                    "info": info0, "lots": side_sign * m,
                    "entry_price": lot[1], "closed": True,
                    "close_price": price, "fee": m * lot[2] + m * fpu,
                    "entry_dt": lot[3],
                })
                lot[0] -= m
                q -= m
                if lot[0] <= 0:
                    queue.popleft()
            return q

        for ckey, items in groups.items():
            info0 = items[0][1]
            longs: deque = deque()                    # [qty, price, fee_per_unit, entry_dt]
            shorts: deque = deque()
            for d, _info in items:
                qty = d["qty"]
                price = d["price"]
                fpu = (d["fee"] + d["tax"]) / qty if qty else 0.0
                dt = _exec_dt(d)
                trade = d["trade"]
                if trade == "買建":
                    longs.append([qty, price, fpu, dt])
                elif trade == "売建":
                    shorts.append([qty, price, fpu, dt])
                elif trade == "転売":                  # close long
                    rem = _fifo_close(longs, qty, price, fpu, +1, info0)
                    if rem > 0:
                        leftover.append(f"{info0['raw']} 転売{rem:g}(対応建玉なし)")
                elif trade == "買戻":                  # close short
                    rem = _fifo_close(shorts, qty, price, fpu, -1, info0)
                    if rem > 0:
                        leftover.append(f"{info0['raw']} 買戻{rem:g}(対応建玉なし)")
                # else (SQ決済 等) は無視
            for lot in longs:
                if lot[0] > 0:
                    specs.append({"info": info0, "lots": lot[0], "entry_price": lot[1],
                                  "closed": False, "close_price": 0.0, "fee": lot[0] * lot[2],
                                  "entry_dt": lot[3]})
            for lot in shorts:
                if lot[0] > 0:
                    specs.append({"info": info0, "lots": -lot[0], "entry_price": lot[1],
                                  "closed": False, "close_price": 0.0, "fee": lot[0] * lot[2],
                                  "entry_dt": lot[3]})

        # Order specs by the OPEN fill's timestamp so each option and its hedge
        # future (entered back-to-back) stay adjacent → the 合計損益 per-set
        # column pairs option ↔ future correctly. Stable within equal times.
        specs.sort(key=lambda sp: sp["entry_dt"])

        # Replace RSS-imported rows, keep manual ones.
        self.sim_positions = [p for p in self.sim_positions if not p.get("rss_imported")]

        added: int = 0
        closed_n: int = 0
        skipped: list[str] = []
        for spec in specs:
            info = spec["info"]
            lots = int(round(spec["lots"]))
            if lots == 0:
                continue
            chain_symbol = self._find_chain_by_yymm(portfolio, info["yymm"])
            if not chain_symbol:
                skipped.append(f"{info['raw']} (限月{info['yymm']}のチェーンなし)")
                continue
            chain: ChainData | None = portfolio.chains.get(chain_symbol)
            if not chain:
                skipped.append(f"{info['raw']} (チェーン取得失敗)")
                continue

            is_closed: bool = spec["closed"]
            if info["kind"] == "option":
                option = self._find_option_by_strike(chain, info["cp"], info["strike"])
                if option is None:
                    skipped.append(f"{info['raw']} (行使価格{info['strike']}未検出)")
                    continue
                entry_underlying = option.underlying.mid_price if option.underlying else 0
                self.sim_positions.append({
                    "option_data": option,
                    "underlying_data": None,
                    "chain_symbol": chain_symbol,
                    "kind": "option",
                    "cp": option.option_type,
                    "strike": option.strike_price,
                    "lots": lots,
                    "entry_price": spec["entry_price"],
                    "entry_iv": option.mid_impv,
                    "entry_underlying": entry_underlying,
                    "entry_tte": option.time_to_expiry,
                    "entry_time": datetime.now().isoformat(),
                    "entry_delta": option.theo_delta,
                    "entry_gamma": option.theo_gamma,
                    "entry_theta": option.theo_theta,
                    "entry_vega": option.theo_vega,
                    "size": option.size,
                    "label": f"{info['cp']}{option.strike_price:.0f}",
                    "vt_symbol": option.vt_symbol,
                    "enabled": True,
                    "closed": is_closed,
                    "close_price": spec["close_price"],
                    "fee": spec["fee"],
                    "rss_imported": True,
                    "rss_batch": 0,
                })
            else:  # futures
                underlying = chain.underlying
                if underlying is None:
                    skipped.append(f"{info['raw']} (先物原資産なし)")
                    continue
                futures_multiplier = 0.1 if info["is_mini"] else 1.0
                futures_type = "先物ミニ" if info["is_mini"] else "先物ラージ"
                self.sim_positions.append({
                    "option_data": None,
                    "underlying_data": underlying,
                    "chain_symbol": chain_symbol,
                    "kind": "futures",
                    "cp": 0,
                    "strike": 0,
                    "lots": lots,
                    "entry_price": spec["entry_price"],
                    "entry_iv": 0,
                    "entry_underlying": spec["entry_price"],
                    "entry_tte": 0,
                    "entry_time": datetime.now().isoformat(),
                    "entry_delta": underlying.size * futures_multiplier,
                    "entry_gamma": 0,
                    "entry_theta": 0,
                    "entry_vega": 0,
                    "size": underlying.size,
                    "futures_multiplier": futures_multiplier,
                    "label": f"{futures_type} {chain_symbol.split('.')[0]}",
                    "vt_symbol": underlying.vt_symbol,
                    "enabled": True,
                    "closed": is_closed,
                    "close_price": spec["close_price"],
                    "fee": spec["fee"],
                    "rss_imported": True,
                    "rss_batch": 0,
                })
            added += 1
            if is_closed:
                closed_n += 1

        self._refresh_sim_table()
        self._save_settings()

        # Summary
        msg = (f"RSS約定を再構成: {added}件（うち決済み {closed_n}件）。\n"
               f"今回読込 {len(executions)}件（新規 {added_new}件）/ 累計 {len(self._rss_ledger)}件を保持中。")
        if leftover:
            msg += "\n\n[対応建玉なしの決済 " + str(len(leftover)) + "件]\n" + "\n".join(leftover[:10])
        if skipped:
            msg += "\n\n[未マッチ " + str(len(skipped)) + "件]\n" + "\n".join(skipped[:10])
        if unparsed:
            msg += "\n\n[銘柄名称の解析不可 " + str(len(unparsed)) + "件]\n" + "\n".join(unparsed[:10])
        QtWidgets.QMessageBox.information(self, "RSS取込", msg, QtWidgets.QMessageBox.Ok)

    def _clear_rss_history(self) -> None:
        """Clear the RSS execution ledger and remove all RSS-imported rows."""
        n_ledger: int = len(self._rss_ledger)
        n_rows: int = sum(1 for p in self.sim_positions if p.get("rss_imported"))
        if n_ledger == 0 and n_rows == 0:
            QtWidgets.QMessageBox.information(
                self, "RSS履歴クリア", "クリアする履歴はありません。",
                QtWidgets.QMessageBox.Ok,
            )
            return
        reply = QtWidgets.QMessageBox.question(
            self, "RSS履歴クリア",
            f"RSS約定履歴 {n_ledger}件 と RSS取込行 {n_rows}件 を削除します。\n"
            "よろしいですか？（手動追加した行は残ります）",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._rss_ledger = []
        self.sim_positions = [p for p in self.sim_positions if not p.get("rss_imported")]
        self._refresh_sim_table()
        self._save_settings()

    def _remove_sim_row(self) -> None:
        row: int = self.sim_table.currentRow()
        if 0 <= row < len(self.sim_positions):
            self.sim_positions.pop(row)
            self._refresh_sim_table()

    def _refresh_sim_table(self) -> None:
        """Rebuild the sim table display from self.sim_positions."""
        # Identify the cell currently being edited (if any) so the live-price
        # refresh doesn't clobber the user's in-progress input.
        editing_row: int = -1
        editing_col: int = -1
        if self.sim_table.state() == QtWidgets.QAbstractItemView.State.EditingState:
            idx = self.sim_table.currentIndex()
            if idx.isValid():
                editing_row = idx.row()
                editing_col = idx.column()

        # Block itemChanged signals during rebuild
        self.sim_table.blockSignals(True)
        self.sim_table.setRowCount(len(self.sim_positions))
        total_pnl: float = 0.0
        realized_pnl: float = 0.0
        # Per-row P&L and option/futures flag, for the per-set 合計損益 column.
        row_pnls: list[float] = []
        row_is_option: list[bool] = []
        # Per-row futures diff (current − entry price); None for option rows.
        row_fut_diffs: list[float | None] = []

        for row, pos in enumerate(self.sim_positions):
            month_display: str = pos["chain_symbol"].split(".")[0]
            opt: OptionData | None = pos["option_data"]
            und: UnderlyingData | None = pos["underlying_data"]
            entry_price: float = pos.get("entry_price", 0)
            entry_iv: float = pos.get("entry_iv", 0)
            entry_underlying: float = pos.get("entry_underlying", 0)
            entry_tte: float = pos.get("entry_tte", 0)
            entry_delta: float = pos.get("entry_delta", 0)
            entry_gamma: float = pos.get("entry_gamma", 0)
            entry_theta: float = pos.get("entry_theta", 0)
            entry_vega: float = pos.get("entry_vega", 0)
            lots: int = pos["lots"]
            size: int = pos["size"]
            enabled: bool = pos.get("enabled", True)
            closed: bool = pos.get("closed", False)
            close_price: float = pos.get("close_price", 0.0)
            fee: float = pos.get("fee", 0.0)          # 手数料 + 税金 (cost)

            # Current greeks
            cur_delta: float = 0
            cur_gamma: float = 0
            cur_theta: float = 0
            cur_vega: float = 0
            cur_underlying: float = 0
            cur_tte: float = 0
            cur_iv_dec: float = 0

            if pos["kind"] == "futures":
                fm: float = pos.get("futures_multiplier", 1.0)
                type_str = pos.get("label", "先物").split(" ")[0]
                strike_str = "-"
                # Fall back to the hand-input 現在値 when no live price (e.g.
                # out of NK225_OP_STRIKE_SCOPE → no updates).
                cur_price: float = (
                    self._snap_price(und.mid_price, "futures", fm < 1.0)
                    if und and und.mid_price
                    else pos.get("manual_price", 0.0)
                )
                current_price_str = f"{cur_price:.0f}" if cur_price else ""
                entry_iv_str = "-"
                current_iv = "-"
                pnl: float = (cur_price - entry_price) * lots * size * fm if cur_price else 0
                cur_delta = und.size * fm if und else 0
                cur_underlying = cur_price
            else:
                fm = 1.0
                type_str = "コール" if pos["cp"] > 0 else "プット"
                strike_str = f"{pos['strike']:.0f}"
                # Fall back to the hand-input 現在値 when no live price (e.g.
                # out of NK225_OP_STRIKE_SCOPE → no updates).
                cur_price = (
                    self._snap_price(opt.mid_price, "option")
                    if opt and opt.mid_price
                    else pos.get("manual_price", 0.0)
                )
                current_price_str = f"{cur_price:.1f}" if cur_price else ""
                entry_iv_str = f"{entry_iv * 100:.2f}" if entry_iv else "-"
                current_iv = f"{opt.mid_impv * 100:.2f}" if opt and opt.mid_impv else ""
                pnl = (cur_price - entry_price) * lots * size if cur_price else 0
                if opt:
                    cur_delta = opt.theo_delta
                    cur_gamma = opt.theo_gamma
                    cur_theta = opt.theo_theta
                    cur_vega = opt.theo_vega
                    cur_underlying = opt.underlying.mid_price if opt.underlying else 0
                    cur_tte = opt.time_to_expiry
                    cur_iv_dec = opt.mid_impv or 0

            # Override PnL for closed positions: realized (close_price - entry_price)
            if closed:
                pnl = (close_price - entry_price) * lots * size * fm

            # Estimated Rakuten trading fee (税込) for closing an OPEN position.
            # Closed rows already have their actual close fee inside `fee`.
            trade_fee: float = 0.0
            if not closed:
                est_price: float = cur_price if cur_price else entry_price
                trade_fee = self._estimate_trade_fee_yen(pos, est_price)

            # Subtract commission + tax (手数料+税金) and the estimated close fee
            # from the P&L. Fees are in yen while P&L is in 千円, so scale /1000.
            pnl -= fee / 1000.0
            pnl -= trade_fee / 1000.0

            if enabled:
                if closed:
                    realized_pnl += pnl
                else:
                    total_pnl += pnl

            # Contributions (Taylor decomposition of PnL: entry → current)
            ds: float = cur_underlying - entry_underlying if cur_underlying and entry_underlying else 0
            # Elapsed days from entry_time (wall clock) for theta decay
            dt_days: float = 0
            entry_time_str: str = pos.get("entry_time", "")
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    dt_days = (datetime.now() - entry_dt).total_seconds() / 86400.0
                except ValueError:
                    pass
            dv_pct: float = (cur_iv_dec - entry_iv) * 100 if pos["kind"] != "futures" else 0
            delta_contrib: float = entry_delta * ds * lots
            gamma_contrib: float = 0.5 * entry_gamma * ds * ds * lots
            theta_contrib: float = entry_theta * dt_days * lots
            vega_contrib: float = entry_vega * dv_pct * lots

            has_pnl: bool = bool(cur_price or closed or fee or trade_fee)
            pnl_str: str = f"{pnl:.1f}" if has_pnl else ""

            # Track for the per-set columns (filled after the loop).
            row_pnls.append(pnl if has_pnl else 0.0)
            is_futures_row: bool = pos["kind"] == "futures"
            row_is_option.append(not is_futures_row)
            if is_futures_row and cur_price:
                row_fut_diffs.append(cur_price - entry_price)
            else:
                row_fut_diffs.append(None)

            # Column 0: checkbox for enabled state
            check_item = QtWidgets.QTableWidgetItem()
            check_item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsUserCheckable
                | QtCore.Qt.ItemFlag.ItemIsEnabled
            )
            check_item.setCheckState(
                QtCore.Qt.CheckState.Checked if enabled
                else QtCore.Qt.CheckState.Unchecked
            )
            self.sim_table.setItem(row, 0, check_item)

            def fmt(v: float, prec: int = 2) -> str:
                return f"{v:.{prec}f}" if v else "-"

            # IV差分 (%)
            iv_diff: float = dv_pct if pos["kind"] != "futures" else 0
            iv_diff_str: str = f"{iv_diff:+.2f}" if pos["kind"] != "futures" and cur_iv_dec else "-"

            # Entry time display (MM/DD HH:MM)
            entry_time_display: str = ""
            if entry_time_str:
                try:
                    entry_dt_disp = datetime.fromisoformat(entry_time_str)
                    entry_time_display = entry_dt_disp.strftime("%m/%d %H:%M")
                except ValueError:
                    pass

            values: list[str] = [
                month_display, type_str, strike_str,
                str(lots),
                entry_time_display,
                f"{entry_price:.1f}" if entry_price else "",
                current_price_str,
                entry_iv_str,
                current_iv,
                iv_diff_str,
                "",         # 先物差 (per-set) — filled after the loop
                "",         # 合計損益 (per-set) — filled after the loop
                pnl_str,
                # Contributions (moved after 損益)
                f"{delta_contrib:.1f}",
                f"{gamma_contrib:.1f}",
                f"{theta_contrib:.1f}",
                f"{vega_contrib:.1f}",
                # Entry greeks (per-position: multiplied by lots)
                fmt(entry_delta * lots, 2),
                fmt(entry_gamma * lots, 6),
                fmt(entry_theta * lots, 2),
                fmt(entry_vega * lots, 2),
                # Current greeks (per-position: multiplied by lots)
                fmt(cur_delta * lots, 2),
                fmt(cur_gamma * lots, 6),
                fmt(cur_theta * lots, 2),
                fmt(cur_vega * lots, 2),
            ]
            # Columns with red/green coloring (plus=red, minus=green)
            # Indices after inserting 先物差(11) & 合計損益(12): 損益=13, 寄与=14-17
            pnl_col: int = 13
            contrib_cols: set[int] = {14, 15, 16, 17}
            for i, val in enumerate(values):
                col = i + 1
                if row == editing_row and col == editing_col:
                    continue  # leave the in-edit cell alone
                item = QtWidgets.QTableWidgetItem(val)
                if col in (4, 6, 7):  # 枚数 / 建値 / 現在値(手入力) columns are editable
                    item.setFlags(
                        QtCore.Qt.ItemFlag.ItemIsEnabled
                        | QtCore.Qt.ItemFlag.ItemIsSelectable
                        | QtCore.Qt.ItemFlag.ItemIsEditable
                    )
                else:
                    item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if (col == pnl_col or col in contrib_cols) and val and val != "-":
                    try:
                        num = float(val)
                        if num > 0:
                            item.setForeground(QtGui.QColor(255, 100, 100))
                        elif num < 0:
                            item.setForeground(QtGui.QColor(100, 255, 100))
                    except ValueError:
                        pass
                self.sim_table.setItem(row, col, item)

            # Column 26: 決済済 checkbox
            closed_item = QtWidgets.QTableWidgetItem()
            closed_item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsUserCheckable
                | QtCore.Qt.ItemFlag.ItemIsEnabled
            )
            closed_item.setCheckState(
                QtCore.Qt.CheckState.Checked if closed
                else QtCore.Qt.CheckState.Unchecked
            )
            self.sim_table.setItem(row, 26, closed_item)

            # Column 27: 決済値 editable
            close_val_str: str = f"{close_price:.1f}" if close_price else ""
            close_val_item = QtWidgets.QTableWidgetItem(close_val_str)
            close_val_item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled
                | QtCore.Qt.ItemFlag.ItemIsSelectable
                | QtCore.Qt.ItemFlag.ItemIsEditable
            )
            self.sim_table.setItem(row, 27, close_val_item)

            # Column 28: 手数料 (手数料+税金) in 千円 — read-only cost, already
            # subtracted from the 損益 / 合計損益 columns.
            fee_item = QtWidgets.QTableWidgetItem(f"{fee / 1000.0:.3f}" if fee else "")
            fee_item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            self.sim_table.setItem(row, 28, fee_item)

            # Column 29: 取引手数料(税込) — estimated close fee (千円) for OPEN
            # positions, already subtracted from 損益 / 合計損益.
            tfee_item = QtWidgets.QTableWidgetItem(
                f"{trade_fee / 1000.0:.3f}" if trade_fee else ""
            )
            tfee_item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            self.sim_table.setItem(row, 29, tfee_item)

        # Per-set columns: a set = 1 option row + the 1 following futures row;
        # a lone option (next row is another option) is its own set; an orphan
        # futures row is its own set. Values are shown on the set's leading row.
        #   先物差 (col 11)  = futures leg's (current − entry) in the set
        #   合計損益 (col 12) = sum of the set's rows' P&L
        def _colored_item(text: str, value: float) -> QtWidgets.QTableWidgetItem:
            it = QtWidgets.QTableWidgetItem(text)
            it.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            if value > 0:
                it.setForeground(QtGui.QColor(255, 100, 100))
            elif value < 0:
                it.setForeground(QtGui.QColor(100, 255, 100))
            return it

        n_rows: int = len(self.sim_positions)
        i: int = 0
        while i < n_rows:
            if row_is_option[i] and i + 1 < n_rows and not row_is_option[i + 1]:
                set_rows = [i, i + 1]
                i += 2
            else:
                set_rows = [i]
                i += 1
            leader: int = set_rows[0]

            # 先物差: futures diff within the set (blank if no futures row)
            fut_vals: list[float] = [
                row_fut_diffs[r] for r in set_rows if row_fut_diffs[r] is not None
            ]
            if fut_vals:
                fut_total: float = sum(fut_vals)
                self.sim_table.setItem(
                    leader, 11, _colored_item(f"{fut_total:+.0f}", fut_total)
                )

            # 合計損益: sum of the set's P&L
            set_total: float = sum(row_pnls[r] for r in set_rows)
            self.sim_table.setItem(
                leader, 12, _colored_item(f"{set_total:.1f}", set_total)
            )

        self.sim_table.blockSignals(False)

        # Update total P&L label (unrealized)
        if total_pnl > 0:
            color = "color: #ff6464;"
        elif total_pnl < 0:
            color = "color: #64ff64;"
        else:
            color = ""
        self.sim_total_pnl_label.setText(f"合計損益: {total_pnl:.3f}")
        self.sim_total_pnl_label.setStyleSheet(f"font-weight: bold; font-size: 13px; {color}")

        # Update realized P&L label (実現損益)
        if realized_pnl > 0:
            r_color = "color: #ff6464;"
        elif realized_pnl < 0:
            r_color = "color: #64ff64;"
        else:
            r_color = ""
        self.sim_realized_pnl_label.setText(f"実現損益: {realized_pnl:.3f}")
        self.sim_realized_pnl_label.setStyleSheet(f"font-weight: bold; font-size: 13px; {r_color}")

        # Auto-size columns to content for a compact layout
        self.sim_table.resizeColumnsToContents()

    def _on_sim_table_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        """Handle checkbox toggle and 枚数 edit in the sim table."""
        col: int = item.column()
        row: int = item.row()
        if not (0 <= row < len(self.sim_positions)):
            return

        if col == 0:
            new_enabled: bool = item.checkState() == QtCore.Qt.CheckState.Checked
            if self.sim_positions[row].get("enabled", True) == new_enabled:
                return
            self.sim_positions[row]["enabled"] = new_enabled
            self._refresh_sim_table()
            self._run_sim_analysis()
        elif col == 4:  # 枚数 column
            try:
                new_lots: int = int(item.text())
            except ValueError:
                self._refresh_sim_table()
                return
            if self.sim_positions[row].get("lots", 0) == new_lots:
                return
            self.sim_positions[row]["lots"] = new_lots
            self._refresh_sim_table()
            self._run_sim_analysis()
        elif col == 6:  # 建値 column
            text: str = item.text().strip()
            try:
                new_entry_price: float = float(text) if text else 0.0
            except ValueError:
                self._refresh_sim_table()
                return
            if self.sim_positions[row].get("entry_price", 0.0) == new_entry_price:
                return
            self.sim_positions[row]["entry_price"] = new_entry_price
            self._refresh_sim_table()
            self._run_sim_analysis()
        elif col == 7:  # 現在値 (手入力) — used when no live price is available
            text: str = item.text().strip()
            try:
                new_manual_price: float = float(text) if text else 0.0
            except ValueError:
                self._refresh_sim_table()
                return
            if self.sim_positions[row].get("manual_price", 0.0) == new_manual_price:
                return
            self.sim_positions[row]["manual_price"] = new_manual_price
            self._refresh_sim_table()
            self._run_sim_analysis()
        elif col == 26:  # 決済済 checkbox
            new_closed: bool = item.checkState() == QtCore.Qt.CheckState.Checked
            pos_row = self.sim_positions[row]
            if pos_row.get("closed", False) == new_closed:
                return
            pos_row["closed"] = new_closed
            # Auto-fill 決済値 with current mid-price on close toggle (if still blank)
            if new_closed and not pos_row.get("close_price", 0.0):
                opt = pos_row.get("option_data")
                und = pos_row.get("underlying_data")
                live_price: float = 0.0
                if pos_row.get("kind") == "futures":
                    live_price = und.mid_price if und and und.mid_price else 0.0
                else:
                    live_price = opt.mid_price if opt and opt.mid_price else 0.0
                if live_price:
                    pos_row["close_price"] = live_price
            self._refresh_sim_table()
            self._run_sim_analysis()
        elif col == 27:  # 決済値 editable
            text: str = item.text().strip()
            try:
                new_close_price: float = float(text) if text else 0.0
            except ValueError:
                self._refresh_sim_table()
                return
            if self.sim_positions[row].get("close_price", 0.0) == new_close_price:
                return
            self.sim_positions[row]["close_price"] = new_close_price
            self._refresh_sim_table()
            self._run_sim_analysis()

    def _on_update_timer(self) -> None:
        """Periodic refresh for real-time updates in simulation mode."""
        if self.mode_combo.currentIndex() != 1:
            return
        if not self.sim_positions:
            return
        # _refresh_sim_table skips the specific cell that's currently being
        # edited, so live-price updates flow through the rest of the table.
        self._refresh_sim_table()
        self._run_sim_analysis()

    def _get_sim_positions(self) -> list[dict]:
        """Return enabled simulation positions with live data.
        Closed positions are excluded from greek analysis (delta/gamma/theta/vega)."""
        return [
            p for p in self.sim_positions
            if p.get("enabled", True) and not p.get("closed", False)
        ]

    def _get_pricing_model(self):
        """Get the pricing model from the portfolio."""
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        return portfolio.pricing_model

    def _calculate_sim_pnl_at_price(
        self,
        positions: list[dict],
        sim_price: float,
        ref_price: float,
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[float, float, float, float, dict[int, float]]:
        """Calculate P&L for simulation positions using live OptionData."""
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)
        pricing_model = self._get_pricing_model()

        total_now: float = 0.0
        total_day_same: float = 0.0
        total_day_adj: float = 0.0
        total_expiry: float = 0.0
        leg_pnls: dict[int, float] = {}

        for i, pos in enumerate(positions):
            lots: int = pos["lots"]
            size: int = pos["size"]
            entry_price: float = pos.get("entry_price", 0)

            if pos["kind"] == "futures":
                fm: float = pos.get("futures_multiplier", 1.0)
                # PnL based on entry price (建値)
                pnl: float = (sim_price - entry_price) * lots * size * fm
                total_now += pnl
                total_day_same += pnl
                total_day_adj += pnl
                total_expiry += pnl
                leg_pnls[i] = pnl
            else:
                opt: OptionData | None = pos.get("option_data")
                if not opt or not opt.mid_impv:
                    continue

                cp: int = opt.option_type
                strike: float = opt.strike_price
                iv: float = opt.mid_impv
                rate: float = opt.interest_rate
                tte: float = opt.time_to_expiry
                adj: float = opt.underlying_adjustment
                new_underlying: float = sim_price + adj

                # Baseline: entry price (建値) at time of position entry
                base: float = entry_price

                # Scenario 1: current time, current IV
                p_now: float = pricing_model.calculate_price(
                    new_underlying, strike, rate, tte, iv, cp
                )
                total_now += (p_now - base) * lots * size

                # Scenario 2: +N days, same IV
                new_t: float = max(tte - time_change, 1e-6)
                p_day: float = pricing_model.calculate_price(
                    new_underlying, strike, rate, new_t, iv, cp
                )
                total_day_same += (p_day - base) * lots * size

                # Scenario 3: +N days, IV adjusted
                new_iv: float = max(iv + iv_adj, 0.01)
                p_adj: float = pricing_model.calculate_price(
                    new_underlying, strike, rate, new_t, new_iv, cp
                )
                total_day_adj += (p_adj - base) * lots * size

                # Scenario 4: expiration
                intrinsic: float = max(0.0, cp * (new_underlying - strike))
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

            new_underlying: float = option.underlying.mid_price * price_ratio + option.underlying_adjustment
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
        step_sigma: float = self._step_sigma()
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

        # Step-level values for the results table (descending price grid)
        price_levels: list[tuple[float, str]] = self._build_price_levels(
            ref_price, None, None, price_range, step_sigma,
        )

        # Base Greeks at ref_price for Taylor decomposition (contributions)
        base_g, _, _ = self._calculate_portfolio_greeks_at_price(
            portfolio, ref_price, ref_price, 0.0, 0.0,
        )

        table_data: list[dict] = []
        for sp, _lbl in price_levels:
            now, day_same, day_adj, expiry, _ = self._calculate_pnl_at_price(
                portfolio, sp, ref_price, time_change, iv_per_1000
            )
            iv_change_pct: float = iv_per_1000 * ((sp - ref_price) / 1000.0)

            # Greek values at sim_price (current time, current IV)
            g_at_p, _, _ = self._calculate_portfolio_greeks_at_price(
                portfolio, sp, ref_price, 0.0, 0.0,
            )

            # Taylor contributions around ref state
            ds: float = sp - ref_price
            delta_contrib: float = base_g["delta"] * ds
            gamma_contrib: float = 0.5 * base_g["gamma"] * ds * ds
            theta_contrib: float = base_g["theta"] * days
            vega_contrib: float = base_g["vega"] * iv_change_pct

            table_data.append({
                "price": sp,
                "iv_change": iv_change_pct,
                "pnl_now": now / 1000,
                "pnl_day_same": day_same / 1000,
                "pnl_day_adj": day_adj / 1000,
                "pnl_expiry": expiry / 1000,
                "delta": g_at_p["delta"] / 1000,
                "gamma": g_at_p["gamma"] / 1000,
                "theta": g_at_p["theta"] / 1000,
                "vega": g_at_p["vega"] / 1000,
                "iv_value": iv_change_pct,
                "delta_contrib": delta_contrib / 1000,
                "gamma_contrib": gamma_contrib / 1000,
                "theta_contrib": theta_contrib / 1000,
                "vega_contrib": vega_contrib / 1000,
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
            return

        # Determine ref_price from the first position's underlying
        ref_price: float = 0.0
        for pos in positions:
            opt: OptionData | None = pos.get("option_data")
            und: UnderlyingData | None = pos.get("underlying_data")
            if opt and opt.underlying and opt.underlying.mid_price:
                ref_price = opt.underlying.mid_price
                break
            elif und and und.mid_price:
                ref_price = und.mid_price
                break
        if not ref_price:
            return

        price_range: int = self.price_range_spin.value()
        step_sigma: float = self._step_sigma()
        days: int = self.days_spin.value()
        time_change: float = days / ANNUAL_DAYS
        show_legs: bool = self.show_legs_check.isChecked()

        # ATM-IV daily band basis (前日終値 & 日次ATM IV): drives the σ price grid
        # and the interpretation of IV感応度 (%IV per +0.5σ).
        tbl_symbol: str = positions[0]["chain_symbol"]
        tbl_base: float | None = self._load_prev_day_close(tbl_symbol.split(".")[0])
        tbl_chain = self.option_engine.get_portfolio(self.portfolio_name).chains.get(tbl_symbol)
        tbl_div: float | None = (
            tbl_chain.atm_impv / (252 ** 0.5) if tbl_chain and tbl_chain.atm_impv else None
        )

        # IV感応度 input = %IV per +0.5σ futures move → convert to the internal
        # %/1000pt used by the P&L engine.
        iv_sens: float = self.iv_sensitivity_spin.value()
        if tbl_base and tbl_div:
            price_per_half_sigma: float = 0.5 * tbl_div * tbl_base
            iv_per_1000: float = (
                iv_sens * 1000.0 / price_per_half_sigma if price_per_half_sigma else 0.0
            )
        else:
            iv_per_1000 = iv_sens

        # Total fees (千円) for enabled/open positions, subtracted from every P&L
        # curve & table row so they match the sim table's top 合計損益 (net of
        # fees). Split into 手数料 (実費) and 取引手数料 (決済見積り) for display.
        fee_actual_yen: float = 0.0
        trade_fee_yen: float = 0.0
        for pos in positions:
            fee_actual_yen += pos.get("fee", 0.0)
            if pos["kind"] == "futures":
                _und = pos.get("underlying_data")
                _cp: float = (
                    self._snap_price(_und.mid_price, "futures",
                                     pos.get("futures_multiplier", 1.0) < 1.0)
                    if _und and _und.mid_price else pos.get("manual_price", 0.0)
                )
            else:
                _opt = pos.get("option_data")
                _cp = (
                    self._snap_price(_opt.mid_price, "option")
                    if _opt and _opt.mid_price else pos.get("manual_price", 0.0)
                )
            trade_fee_yen += self._estimate_trade_fee_yen(
                pos, _cp if _cp else pos.get("entry_price", 0)
            )
        fee_actual_k: float = fee_actual_yen / 1000.0
        trade_fee_k: float = trade_fee_yen / 1000.0
        total_fee_k: float = fee_actual_k + trade_fee_k

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
                positions, sp, ref_price, time_change, iv_per_1000,
            )
            pnl_now_arr.append(now - total_fee_k)
            pnl_day_same_arr.append(day_same - total_fee_k)
            pnl_day_adj_arr.append(day_adj - total_fee_k)
            pnl_expiry_arr.append(expiry - total_fee_k)

            for key in leg_pnl_arrs:
                leg_pnl_arrs[key].append(legs.get(key, 0))

            g_now, g_ds, g_da = self._calculate_sim_greeks_at_price(
                positions, sp, ref_price, time_change, iv_per_1000,
            )
            for gk in ("delta", "gamma", "theta", "vega"):
                greeks_data[gk]["now"].append(g_now[gk])
                greeks_data[gk]["day_same"].append(g_ds[gk])
                greeks_data[gk]["day_adj"].append(g_da[gk])

        # Result-table price rows: σ grid anchored at 前日終値, within
        # [ref ± 価格範囲], stepped by ステップ(σ), descending, incl. 現在/前終.
        price_levels: list[tuple[float, str]] = self._build_price_levels(
            ref_price, tbl_base, tbl_div, price_range, step_sigma,
        )

        # Base Greeks at ref_price for Taylor decomposition (contributions)
        base_g, _, _ = self._calculate_sim_greeks_at_price(
            positions, ref_price, ref_price, 0.0, 0.0,
        )

        # Entry PnL (建値差金): gain/loss from entry prices at current underlying
        entry_pnl, _, _, _, _ = self._calculate_sim_pnl_at_price(
            positions, ref_price, ref_price, 0.0, 0.0,
        )

        table_data: list[dict] = []
        for sp, iv_level in price_levels:
            now, day_same, day_adj, expiry, _ = self._calculate_sim_pnl_at_price(
                positions, sp, ref_price, time_change, iv_per_1000,
            )
            iv_change_pct: float = iv_per_1000 * ((sp - ref_price) / 1000.0)

            # Greek values at sim_price (current time, current IV)
            g_at_p, _, _ = self._calculate_sim_greeks_at_price(
                positions, sp, ref_price, 0.0, 0.0,
            )

            # Taylor contributions around ref state
            ds: float = sp - ref_price
            delta_contrib: float = base_g["delta"] * ds
            gamma_contrib: float = 0.5 * base_g["gamma"] * ds * ds
            theta_contrib: float = base_g["theta"] * days
            vega_contrib: float = base_g["vega"] * iv_change_pct

            table_data.append({
                "price": sp,
                "iv_level": iv_level,
                "fut_diff": sp - ref_price,
                "iv_change": iv_change_pct,
                "pnl_now": now - total_fee_k,
                "pnl_day_same": day_same - total_fee_k,
                "pnl_day_adj": day_adj - total_fee_k,
                "pnl_expiry": expiry - total_fee_k,
                "fee_actual": fee_actual_k,
                "fee_trade": trade_fee_k,
                "delta": g_at_p["delta"],
                "gamma": g_at_p["gamma"],
                "theta": g_at_p["theta"],
                "vega": g_at_p["vega"],
                "iv_value": iv_change_pct,
                "entry_pnl": entry_pnl,
                "delta_contrib": delta_contrib,
                "gamma_contrib": gamma_contrib,
                "theta_contrib": theta_contrib,
                "vega_contrib": vega_contrib,
            })

        # Greeks summary for simulation
        greeks_str = self._calc_sim_greeks_str(positions, ref_price)

        # Collect checked (enabled) futures entry prices for vertical markers
        futures_entries: list[tuple[float, int]] = []
        for pos in positions:
            if pos["kind"] == "futures" and pos.get("entry_price"):
                futures_entries.append((pos["entry_price"], pos["lots"]))

        # Current-value label figures (positions are already enabled & open):
        #   現在損益 = Σ (current price − entry price)×lots×size, using each
        #             leg's ACTUAL current mid price so it matches the table's
        #             合計損益 and is correct per-month (the payoff curve reprices
        #             every leg on a single underlying axis, which drifts for a
        #             second month, so we don't interpolate it here).
        #   先物差   = current futures price − entry price (futures legs only)
        #   IV差     = Σ per-leg IV差分 (matching the sim table's IV差分 column)
        sum_fut_diff: float = 0.0
        sum_iv_diff: float = 0.0
        current_total_pnl: float = 0.0
        sum_fee: float = 0.0
        sum_trade_fee: float = 0.0
        for pos in positions:
            lots: int = pos["lots"]
            size: int = pos["size"]
            entry_price: float = pos.get("entry_price", 0) or 0
            sum_fee += pos.get("fee", 0.0)          # 手数料 + 税金 (cost)
            # Fall back to the hand-input 現在値 (manual_price) when no live
            # price (e.g. out of NK225_OP_STRIKE_SCOPE → no updates).
            manual_price: float = pos.get("manual_price", 0.0) or 0.0
            if pos["kind"] == "futures":
                fm: float = pos.get("futures_multiplier", 1.0)
                und = pos.get("underlying_data")
                cur_price: float = (
                    self._snap_price(und.mid_price, "futures", fm < 1.0)
                    if und and und.mid_price else manual_price
                )
                if cur_price:
                    current_total_pnl += (cur_price - entry_price) * lots * size * fm
                    if entry_price:
                        sum_fut_diff += cur_price - entry_price
                sum_trade_fee += self._estimate_trade_fee_yen(
                    pos, cur_price if cur_price else entry_price)
            else:
                opt = pos.get("option_data")
                cur_opt_price: float = (
                    self._snap_price(opt.mid_price, "option")
                    if opt and opt.mid_price else manual_price
                )
                if cur_opt_price:
                    current_total_pnl += (cur_opt_price - entry_price) * lots * size
                sum_trade_fee += self._estimate_trade_fee_yen(
                    pos, cur_opt_price if cur_opt_price else entry_price)
                entry_iv: float = pos.get("entry_iv", 0) or 0
                cur_iv: float = (opt.mid_impv or 0) if opt else 0
                if cur_iv and entry_iv:
                    sum_iv_diff += (cur_iv - entry_iv) * 100

        # Subtract commission + tax and the estimated close fee so 現在損益
        # matches the table's 合計損益. Fees are in yen, P&L in 千円 → /1000.
        current_total_pnl -= sum_fee / 1000.0
        current_total_pnl -= sum_trade_fee / 1000.0

        # Make the 現在 row's 現在P&L exactly equal the sim table's 合計損益 /
        # 現在損益 label: use the live snapped-mid figure (current_total_pnl)
        # instead of the model reprice at ref (which differs by the futures
        # snapping and model-vs-mid).
        for _tr in table_data:
            if _tr.get("iv_level") == "現在":
                _tr["pnl_now"] = current_total_pnl
                break

        # Base price (prev-day futures close) + ATM daily IV for vertical bands,
        # taken from the reference (first) position's chain.
        base_price: float | None = None
        daily_iv: float | None = None
        ref_chain_symbol: str = positions[0]["chain_symbol"]
        base_price = self._load_prev_day_close(ref_chain_symbol.split(".")[0])
        portfolio_data: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        ref_chain: ChainData | None = portfolio_data.chains.get(ref_chain_symbol)
        if ref_chain and ref_chain.atm_impv:
            daily_iv = ref_chain.atm_impv / (252 ** 0.5)

        self._update_chart(
            prices_fine, pnl_now_arr, pnl_day_same_arr, pnl_day_adj_arr,
            pnl_expiry_arr, leg_pnl_arrs, leg_labels, ref_price, days,
            show_legs, greeks_str, futures_entries=futures_entries,
            fut_diff=sum_fut_diff, iv_diff=sum_iv_diff,
            current_pnl_value=current_total_pnl,
            base_price=base_price, daily_iv=daily_iv,
        )
        self._update_greeks_charts(
            prices_fine, greeks_data, ref_price, days,
            is_sim=True, futures_entries=futures_entries,
            fut_diff=sum_fut_diff, iv_diff=sum_iv_diff,
            base_price=base_price, daily_iv=daily_iv,
        )
        self._update_table(table_data, days, is_sim=True)

    def _load_prev_day_close(self, futures_symbol: str) -> float | None:
        """Previous day's futures close (pre_close of the latest minute bar)."""
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=2)
        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_bar_data(
            symbol=futures_symbol,
            exchange=Exchange.JPX,
            interval=Interval.MINUTE,
            start=start,
            end=now,
        )
        if not bars:
            return None
        pre_close: float = bars[-1].pre_close
        return pre_close if pre_close else None

    def _load_futures_daily_close_series(
        self, futures_symbol: str, days: int
    ) -> dict[str, float]:
        """Daily futures close per session date (night session 17:00+ → next day)."""
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=days)
        database: BaseDatabase = get_database()
        bars: list[BarData] = database.load_bar_data(
            symbol=futures_symbol, exchange=Exchange.JPX,
            interval=Interval.MINUTE, start=start, end=now,
        )
        close_by_date: dict[str, float] = {}
        for bar in bars:
            session_dt = bar.datetime + timedelta(days=1) if bar.datetime.hour >= 17 else bar.datetime
            close_by_date[session_dt.strftime("%Y-%m-%d")] = bar.close_price
        return close_by_date

    def _estimate_iv_per_1000(self, chain_symbol: str, days: int) -> float | None:
        """Spot-vol beta: regress daily ΔATM_IV(%) on Δfutures, scaled per 1000pt.

        Returns %-IV change per 1000 futures points (negative for the usual
        equity-index leverage effect), or None if there isn't enough data.
        """
        month: str = chain_symbol.split(".")[0].split("-")[1]

        # ATM IV (Δ0.5) per session date, in annualized % points.
        all_bars: list[BarData] = _load_option_bars_with_today(days)
        month_bars: list[BarData] = _filter_bars_by_month(all_bars, month)
        date_bars: dict[str, list[BarData]] = {}
        for bar in month_bars:
            if bar.iv <= 0 or bar.delta == 0:
                continue
            date_bars.setdefault(bar.datetime.strftime("%Y-%m-%d"), []).append(bar)
        atm_iv_by_date: dict[str, float] = {}
        for date_key, day_bars in date_bars.items():
            iv: float | None = _interpolate_iv_at_delta(day_bars, -0.5)
            if iv and iv > 0:
                atm_iv_by_date[date_key] = iv * 100.0

        # Futures close per session date.
        fut_close: dict[str, float] = self._load_futures_daily_close_series(
            chain_symbol.split(".")[0], days
        )

        dates: list[str] = sorted(set(atm_iv_by_date) & set(fut_close))
        if len(dates) < 3:
            return None

        # Least-squares slope through the origin over daily changes.
        sxx: float = 0.0
        sxy: float = 0.0
        for k in range(1, len(dates)):
            d_s: float = fut_close[dates[k]] - fut_close[dates[k - 1]]
            d_iv: float = atm_iv_by_date[dates[k]] - atm_iv_by_date[dates[k - 1]]
            sxx += d_s * d_s
            sxy += d_s * d_iv
        if sxx <= 0:
            return None
        return (sxy / sxx) * 1000.0

    def _estimate_iv_per_half_sigma(
        self, chain_symbol: str, days: int, delta_type: str,
        base: float | None, daily_iv: float | None,
    ) -> float | None:
        """Regress intraday IV level(%) on futures price level over the last N
        days for the chosen delta type, and return %IV change per +0.5σ move.

        Uses the ATM / eris Δ0.1 IV recorded on the futures minute bars — the
        same series as 株価チャット's iv_item. delta_type ∈
        {Put Δ0.10, ATM (Δ0.50), Call Δ0.10, 全デルタ}.
        """
        symbol: str = chain_symbol.split(".")[0]     # e.g. nk-2609
        now: datetime = datetime.now(DB_TZ)
        start: datetime = now - timedelta(days=days)
        bars: list[BarData] = get_database().load_bar_data(
            symbol=symbol, exchange=Exchange.JPX,
            interval=Interval.MINUTE, start=start, end=now,
        )
        if not bars:
            return None

        fields_map: dict[str, list[str]] = {
            "Put Δ0.10": ["eris_p_iv"],
            "ATM (Δ0.50)": ["atm_iv"],
            "Call Δ0.10": ["eris_c_iv"],
            "全デルタ": ["eris_p_iv", "atm_iv", "eris_c_iv"],
        }
        fields: list[str] = fields_map.get(delta_type, ["atm_iv"])

        # OLS regression of IV LEVEL(%) on futures price LEVEL over the window —
        # this captures the overall IV↔price co-movement you read off the chart
        # (steeper than tiny per-minute deltas, which are dominated by noise and
        # dilute the slope toward zero). For 全デルタ, average the per-series
        # slopes (put/atm/call sit at different IV levels, so don't pool them).
        slopes: list[float] = []
        for field in fields:
            pts: list[tuple[float, float]] = []
            for bar in bars:
                iv = getattr(bar, field, 0) or 0
                price = bar.close_price
                if iv > 0 and price:
                    pts.append((price, iv * 100.0))
            if len(pts) < 10:
                continue
            n: int = len(pts)
            mx: float = sum(p for p, _ in pts) / n
            my: float = sum(v for _, v in pts) / n
            sxx: float = sum((p - mx) ** 2 for p, _ in pts)
            sxy: float = sum((p - mx) * (v - my) for p, v in pts)
            if sxx > 0:
                slopes.append(sxy / sxx)             # %IV per futures point
        if not slopes:
            return None
        slope_per_point: float = sum(slopes) / len(slopes)
        if base and daily_iv:
            return slope_per_point * (0.5 * daily_iv * base)   # per +0.5σ
        return slope_per_point * 1000.0              # fallback (per 1000pt)

    def _auto_iv_sensitivity(self) -> None:
        """Estimate IV感応度 (%IV/+0.5σ) from recent intraday IV, for the chosen
        参照日数 and Δ種別, and fill the spinbox."""
        positions: list[dict] = self.sim_positions
        chain_symbol: str = ""
        if positions:
            chain_symbol = positions[0]["chain_symbol"]
        else:
            chain_symbol = self.sim_month_combo.currentData() or ""
        if not chain_symbol:
            QtWidgets.QMessageBox.warning(
                self, "自動計算", "対象の限月がありません（ポジションを追加してください）",
                QtWidgets.QMessageBox.Ok,
            )
            return

        days: int = self.iv_auto_days_spin.value()
        delta_type: str = self.iv_auto_delta_combo.currentText()
        base = self._load_prev_day_close(chain_symbol.split(".")[0])
        chain = self.option_engine.get_portfolio(self.portfolio_name).chains.get(chain_symbol)
        div = chain.atm_impv / (252 ** 0.5) if chain and chain.atm_impv else None

        est: float | None = self._estimate_iv_per_half_sigma(
            chain_symbol, days, delta_type, base, div
        )
        if est is None:
            QtWidgets.QMessageBox.warning(
                self, "自動計算",
                "IV感応度を推定できるデータが不足しています。\n"
                "（先物分足のIV記録が参照日数内に十分あるか確認してください）",
                QtWidgets.QMessageBox.Ok,
            )
            return

        # Clamp into the spinbox range and apply.
        lo: float = self.iv_sensitivity_spin.minimum()
        hi: float = self.iv_sensitivity_spin.maximum()
        self.iv_sensitivity_spin.setValue(max(lo, min(hi, est)))
        self._run_sim_analysis()

    def _reset_iv_sensitivity(self) -> None:
        """Set IV感応度 to a simple baseline: the 0.5σ ATM IV変動値 / +0.5σ,
        i.e. 0.5 × 現在の日次ATM IV(%) = 0.5 × (現在ATM IV年率 ÷ √252) × 100.
        This equals 株価チャット iv_item's ATM 0.5σ band (≈0.9%/+0.5σ)."""
        positions: list[dict] = self.sim_positions
        chain_symbol: str = (
            positions[0]["chain_symbol"] if positions
            else (self.sim_month_combo.currentData() or "")
        )
        chain = (
            self.option_engine.get_portfolio(self.portfolio_name).chains.get(chain_symbol)
            if chain_symbol else None
        )
        if not chain or not chain.atm_impv:
            QtWidgets.QMessageBox.warning(
                self, "ATM既定", "現在のATM IVが取得できません（限月・データを確認してください）",
                QtWidgets.QMessageBox.Ok,
            )
            return
        # 0.5σ ATM IV変動値 = 0.5 × 日次ATM IV(%) = 0.5 × (年率ATM IV ÷ √252) × 100.
        # Matches 株価チャット iv_item's ATM 0.5σ band.
        value: float = chain.atm_impv / (252 ** 0.5) * 100.0 * 0.5
        lo: float = self.iv_sensitivity_spin.minimum()
        hi: float = self.iv_sensitivity_spin.maximum()
        self.iv_sensitivity_spin.setValue(max(lo, min(hi, value)))
        self._run_sim_analysis()

    @staticmethod
    def _find_upside_breakeven(
        prices, pnl: list[float], current_price: float
    ) -> float | None:
        """First price above current_price where P&L crosses loss→profit.

        Scans the curve upward from the current price and returns the
        interpolated price of the first negative→positive zero crossing, or
        None if there is none (already profitable upward, or loss everywhere).
        """
        n: int = min(len(prices), len(pnl))
        for i in range(1, n):
            if float(prices[i]) <= current_price:
                continue
            y0: float = pnl[i - 1]
            y1: float = pnl[i]
            if y0 < 0 <= y1:  # loss → profit
                x0: float = float(prices[i - 1])
                x1: float = float(prices[i])
                if y1 == y0:
                    return x1
                return x0 + (0.0 - y0) / (y1 - y0) * (x1 - x0)
        return None

    def _calc_sim_greeks_str(
        self,
        positions: list[dict],
        ref_price: float,
    ) -> str:
        """Calculate aggregate greeks for simulation positions using live data."""
        total_delta: float = 0.0
        total_gamma: float = 0.0
        total_theta: float = 0.0
        total_vega: float = 0.0

        for pos in positions:
            lots: int = pos["lots"]
            size: int = pos["size"]
            if pos["kind"] == "futures":
                fm: float = pos.get("futures_multiplier", 1.0)
                total_delta += lots * size * fm
            else:
                opt: OptionData | None = pos.get("option_data")
                if not opt or not opt.mid_impv:
                    continue
                # Use same greeks as T型报价
                total_delta += opt.theo_delta * lots
                total_gamma += opt.theo_gamma * lots
                total_theta += opt.theo_theta * lots
                total_vega += opt.theo_vega * lots

        return (
            f"Δ:{total_delta:.2f}  Γ:{total_gamma:.6f}  "
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
        futures_entries: list[tuple[float, int]] | None = None,
        fut_diff: float | None = None,
        iv_diff: float | None = None,
        current_pnl_value: float | None = None,
        base_price: float | None = None,
        daily_iv: float | None = None,
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
        if self.show_expiry_check.isChecked():
            self.pnl_ax.plot(
                prices, pnl_expiry, color="#808080", linewidth=1,
                linestyle="--", label="満期",
            )

        # Reference lines
        self.pnl_ax.axvline(
            x=current_price, color="#FF00FF", linestyle=":", alpha=0.9, linewidth=2.2,
            label=f"現在値: {current_price:.0f}",
        )
        self.pnl_ax.axhline(y=0, color="#00E676", linestyle="-", alpha=0.6)

        # Base price (previous day's futures close) + ATM IV daily bands,
        # drawn as vertical lines like vnpy.chart.item.CandleItem._draw_bar_picture:
        # base_price × (1 ± daily_iv × mult) for mult 0.5/1.0/1.5/2.0.
        if base_price:
            self.pnl_ax.axvline(
                x=base_price, color="#FFD700", linestyle=":", alpha=0.9,
                linewidth=2.2, label=f"前日終値: {base_price:.0f}",
            )
            if daily_iv and daily_iv > 0:
                for mult in (0.5, 1.0, 1.5, 2.0):
                    upper: float = base_price * (1 + daily_iv * mult)
                    lower: float = base_price * (1 - daily_iv * mult)
                    self.pnl_ax.axvline(
                        x=upper, color="#FF4B4B", linestyle="--",
                        linewidth=1.5, alpha=0.4,
                    )
                    self.pnl_ax.axvline(
                        x=lower, color="#4BFFFF", linestyle="--",
                        linewidth=1.5, alpha=0.4,
                    )

        # Current P&L: prefer the directly-computed total (matches the table's
        # 合計損益 and is correct per-month); fall back to interpolating the
        # curve when it isn't supplied (e.g. portfolio mode).
        if len(prices) and len(pnl_now):
            if current_pnl_value is not None:
                current_pnl: float = current_pnl_value
            else:
                current_pnl = float(np.interp(current_price, prices, pnl_now))
            pnl_label: str = f"現在損益: {current_pnl:.3f}"
            if fut_diff is not None and iv_diff is not None:
                pnl_label += f"  先物差: {fut_diff:+.0f}  IV差: {iv_diff:+.2f}"
            if base_price:
                pnl_label += f"  先物前日差: {current_price - base_price:+.0f}"
            self.pnl_ax.axhline(
                y=current_pnl, color="#FF66FF", linestyle=":", alpha=0.7,
                label=pnl_label,
            )
            # Value label at the left edge, styled like the mouse cursor label.
            self.pnl_ax.text(
                0.0, current_pnl, f" {pnl_label}",
                transform=self.pnl_ax.get_yaxis_transform(),
                fontsize=11, color="#FF66FF", va="center", ha="left",
                bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=3),
            )

        # Upside break-even of the IV変動 (day_adj) curve: how far the futures
        # must rise before the position stops losing once IV falls with the
        # move (gamma gain overtakes vega loss). Reported vs the current price
        # and as a multiple of the daily ATM IV (前日終値 basis).
        if len(prices) and len(pnl_day_adj) and daily_iv and daily_iv > 0 and base_price:
            be_price = self._find_upside_breakeven(prices, pnl_day_adj, current_price)
            if be_price is not None:
                sigma_mult: float = (be_price - base_price) / (base_price * daily_iv)
                be_text: str = (
                    f"上昇損益分岐(IV変動): {be_price:.0f}"
                    f"　現在比 {be_price - current_price:+.0f}"
                    f"　前日終値比 {sigma_mult:+.2f}×dailyIV"
                )
                be_color: str = "#FFD700"
            else:
                be_text = "上昇損益分岐(IV変動): 上値に交点なし（現状で利益方向 or 全域で損）"
                be_color = "#AAAAAA"
            self.pnl_ax.text(
                0.5, 0.99, be_text, transform=self.pnl_ax.transAxes,
                fontsize=10, color=be_color, ha="center", va="top",
                bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=3),
            )

        # Futures entry price markers
        if futures_entries:
            for entry_price, lots in futures_entries:
                sign: str = "+" if lots > 0 else ""
                self.pnl_ax.axvline(
                    x=entry_price, color="#8A2BE2", linestyle="--", alpha=0.9,
                    linewidth=2.2, label=f"先物建値 {sign}{lots}: {entry_price:.0f}",
                )

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
        time_change: float,
        iv_per_1000: float,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Calculate aggregate Greeks at a simulated price using live OptionData."""
        iv_adj: float = iv_per_1000 / 100.0 * ((sim_price - ref_price) / 1000.0)
        pricing_model = self._get_pricing_model()
        g_now: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_same: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        g_day_adj: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        for pos in positions:
            lots: int = pos["lots"]
            size: int = pos["size"]

            if pos["kind"] == "futures":
                fm: float = pos.get("futures_multiplier", 1.0)
                fd: float = lots * size * fm
                g_now["delta"] += fd
                g_day_same["delta"] += fd
                g_day_adj["delta"] += fd
            else:
                opt: OptionData | None = pos.get("option_data")
                if not opt or not opt.mid_impv:
                    continue

                cp: int = opt.option_type
                strike: float = opt.strike_price
                iv: float = opt.mid_impv
                rate: float = opt.interest_rate
                tte: float = opt.time_to_expiry
                adj: float = opt.underlying_adjustment
                new_underlying: float = sim_price + adj
                new_t: float = max(tte - time_change, 1e-6)
                new_iv: float = max(iv + iv_adj, 0.01)

                # Current time, current IV - use same model as T型报价
                _, delta, gamma, theta, vega = pricing_model.calculate_greeks(
                    new_underlying, strike, rate, tte, iv, cp
                )
                g_now["delta"] += delta * size * lots
                g_now["gamma"] += gamma * size * lots
                g_now["theta"] += theta * size / ANNUAL_DAYS * lots
                g_now["vega"] += vega * size / 100 * lots

                # +N days, same IV
                _, delta, gamma, theta, vega = pricing_model.calculate_greeks(
                    new_underlying, strike, rate, new_t, iv, cp
                )
                g_day_same["delta"] += delta * size * lots
                g_day_same["gamma"] += gamma * size * lots
                g_day_same["theta"] += theta * size / ANNUAL_DAYS * lots
                g_day_same["vega"] += vega * size / 100 * lots

                # +N days, IV adjusted
                _, delta, gamma, theta, vega = pricing_model.calculate_greeks(
                    new_underlying, strike, rate, new_t, new_iv, cp
                )
                g_day_adj["delta"] += delta * size * lots
                g_day_adj["gamma"] += gamma * size * lots
                g_day_adj["theta"] += theta * size / ANNUAL_DAYS * lots
                g_day_adj["vega"] += vega * size / 100 * lots

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
            new_underlying: float = option.underlying.mid_price * price_ratio + option.underlying_adjustment
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
        is_sim: bool = False,
        futures_entries: list[tuple[float, int]] | None = None,
        fut_diff: float | None = None,
        iv_diff: float | None = None,
        base_price: float | None = None,
        daily_iv: float | None = None,
    ) -> None:
        """Plot Delta, Gamma, Theta charts.

        greeks_data structure:
          {"delta": {"now": [...], "day_same": [...], "day_adj": [...]},
           "gamma": {...}, "theta": {...}}
        """
        if is_sim:
            # Simulation mode: data already in correct scale
            conv: dict[str, float] = {
                "delta": 1.0,
                "gamma": 1.0,
                "theta": 1.0,
                "vega": 1.0,
            }
            ylabel: dict[str, str] = {
                "delta": "Delta",
                "gamma": "Gamma",
                "theta": "Theta (/day)",
                "vega": "Vega (/1%IV)",
            }
        else:
            # Portfolio mode: convert yen → 千円
            conv = {
                "delta": 1.0 / 1000.0,
                "gamma": 1.0 / 1000.0,
                "theta": 1.0 / 1000.0,
                "vega": 1.0 / 1000.0,
            }
            ylabel = {
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
                x=current_price, color="#FF00FF", linestyle=":", alpha=0.9, linewidth=2.2,
            )
            ax.axhline(y=0, color="#00E676", linestyle="-", alpha=0.6)

            # Base price (前日終値) + ATM IV daily bands (same as the 損益 tab)
            if base_price:
                ax.axvline(
                    x=base_price, color="#FFD700", linestyle=":", alpha=0.9,
                    linewidth=2.2,
                )
                if daily_iv and daily_iv > 0:
                    for mult in (0.5, 1.0, 1.5, 2.0):
                        ax.axvline(
                            x=base_price * (1 + daily_iv * mult),
                            color="#FF4B4B", linestyle="--", linewidth=1.5, alpha=0.4,
                        )
                        ax.axvline(
                            x=base_price * (1 - daily_iv * mult),
                            color="#4BFFFF", linestyle="--", linewidth=1.5, alpha=0.4,
                        )

            # Futures entry price markers
            if futures_entries:
                for entry_price, lots in futures_entries:
                    sign: str = "+" if lots > 0 else ""
                    ax.axvline(
                        x=entry_price, color="#8A2BE2", linestyle="--",
                        alpha=0.9, linewidth=2.2,
                        label=f"先物建値 {sign}{lots}: {entry_price:.0f}",
                    )

            # Set tick label decimal places per Greek
            decimals: dict[str, int] = {"delta": 2, "gamma": 6, "theta": 1, "vega": 2}
            fmt: str = f"%.{decimals[key]}f"
            ax.yaxis.set_major_formatter(plt.FormatStrFormatter(fmt))

            # Current value: the 現在 curve at the current underlying price,
            # with a cursor-style value label at the left edge.
            if len(prices):
                now_scaled: list[float] = [v * c for v in series["now"]]
                current_g: float = float(np.interp(current_price, prices, now_scaled))
                dec: int = decimals[key]
                g_label: str = f"現在{title}: {current_g:.{dec}f}"
                if fut_diff is not None and iv_diff is not None:
                    g_label += f"  先物差: {fut_diff:+.0f}  IV差: {iv_diff:+.2f}"
                if base_price:
                    g_label += f"  先物前日差: {current_price - base_price:+.0f}"
                ax.axhline(
                    y=current_g, color="#FF66FF", linestyle=":", alpha=0.7,
                    label=g_label,
                )
                ax.text(
                    0.0, current_g, f" {g_label}",
                    transform=ax.get_yaxis_transform(),
                    fontsize=11, color="#FF66FF", va="center", ha="left",
                    bbox=dict(facecolor="black", alpha=0.7, edgecolor="none", pad=3),
                )

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
    def _update_table(self, table_data: list[dict], days: int, is_sim: bool = False) -> None:
        if is_sim:
            greek_headers = ["Δ", "Γ", "Θ(/day)", "V(/1%)", "IV値(%)"]
            contrib_headers = ["建値差金", "Δ寄与", "Γ寄与", "Θ寄与", "V寄与"]
        else:
            greek_headers = ["Δ(千円/pt)", "Γ(千円/pt²)", "Θ(千円/day)", "V(千円/1%)", "IV値(%)"]
            contrib_headers = ["Δ寄与(千円)", "Γ寄与(千円)", "Θ寄与(千円)", "V寄与(千円)"]
        # Sim mode adds an IV水準 label and 先物差 column after 原資産価格,
        # and 手数料 / 取引手数料 columns after 満期P&L (already subtracted from P&L).
        front_headers: list[str] = ["IV水準", "先物差"] if is_sim else []
        fee_headers: list[str] = ["手数料", "取引手数料"] if is_sim else []
        headers: list[str] = [
            "原資産価格", *front_headers, "IV変動(%)",
            "現在P&L", f"+{days}日(同IV)", f"+{days}日(IV変動)", "満期P&L", *fee_headers,
            *greek_headers,
            *contrib_headers,
        ]
        self.result_table.setColumnCount(len(headers))
        self.result_table.setRowCount(len(table_data))
        self.result_table.setHorizontalHeaderLabels(headers)

        for row, data in enumerate(table_data):
            # (text, coloured?) so colouring survives the optional extra columns.
            cells: list[tuple[str, bool]] = [(f"{data['price']:.0f}", False)]
            if is_sim:
                cells.append((data.get("iv_level", ""), False))
                cells.append((f"{data.get('fut_diff', 0):+.0f}", True))
            cells.append((f"{data['iv_change']:+.1f}", False))
            cells.append((f"{data['pnl_now']:.1f}", True))
            cells.append((f"{data['pnl_day_same']:.1f}", True))
            cells.append((f"{data['pnl_day_adj']:.1f}", True))
            cells.append((f"{data['pnl_expiry']:.1f}", True))
            if is_sim:
                cells.append((f"{data.get('fee_actual', 0):.3f}", False))
                cells.append((f"{data.get('fee_trade', 0):.3f}", False))
            cells.append((f"{data['delta']:.2f}", False))
            cells.append((f"{data['gamma']:.6f}", False))
            cells.append((f"{data['theta']:.2f}", False))
            cells.append((f"{data['vega']:.2f}", False))
            cells.append((f"{data['iv_value']:+.2f}", False))
            if is_sim:
                cells.append((f"{data.get('entry_pnl', 0):.1f}", False))
            cells.append((f"{data['delta_contrib']:.1f}", True))
            cells.append((f"{data['gamma_contrib']:.1f}", True))
            cells.append((f"{data['theta_contrib']:.1f}", True))
            cells.append((f"{data['vega_contrib']:.1f}", True))

            for col, (val, coloured) in enumerate(cells):
                item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(val)
                item.setTextAlignment(
                    QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
                if coloured:
                    try:
                        num: float = float(val)
                        if num > 0:
                            item.setForeground(QtGui.QColor(255, 100, 100))
                        elif num < 0:
                            item.setForeground(QtGui.QColor(100, 255, 100))
                    except ValueError:
                        pass
                self.result_table.setItem(row, col, item)

        self.result_table.resizeColumnsToContents()