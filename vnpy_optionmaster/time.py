from datetime import datetime, timedelta
import exchange_calendars


ANNUAL_DAYS = 365

# Get public holidays data from Shanghai Stock Exchange
# cn_calendar: exchange_calendars.ExchangeCalendar = exchange_calendars.get_calendar('XSHG')
jp_calendar = exchange_calendars.get_calendar('XTKS')
holidays: list = [x.to_pydatetime() for x in jp_calendar.regular_holidays.holidays()]

# Filter future public holidays
start: datetime = datetime.today()
PUBLIC_HOLIDAYS = [x for x in holidays if x >= start]


# 残存日数の下限（日）。満期当日の終わりに 0 へ落とすと、IVの逆算も
# ガンマもセータも発散するので、1時間ぶんで止める。
MIN_DAYS_TO_EXPIRY: float = 1.0 / 24.0


def calculate_days_to_expiry(option_expiry: datetime) -> float:
    """満期までの残り日数。時刻まで含めた小数で返す。

    日単位の整数で持つと、0時に残存が1日減った瞬間にIVが段差になる
    （同じ価格でも √(n/(n-1)) 倍に飛ぶ）。時刻まで見れば連続に減る。

    「+1」は元の整数版と揃えるため（満期日の0時で 1日 になる）。0時ちょうど
    の値はこれまでと同じ整数になるので、水準は変わらない。
    """
    remaining: float = (option_expiry - datetime.now()).total_seconds() / 86400.0
    return max(remaining + 1.0, MIN_DAYS_TO_EXPIRY)
