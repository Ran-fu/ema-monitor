//@version=6
strategy(
    "EMA 回踩吞沒策略（雙 TP 分批最終版）v6",
    overlay = true,
    initial_capital = 10000,
    default_qty_type = strategy.percent_of_equity,
    default_qty_value = 10
)

//==================== 參數 ====================
emaFastLen = input.int(12, "EMA 12")
emaMidLen  = input.int(30, "EMA 30（回踩）")
emaSlowLen = input.int(55, "EMA 55（防守）")

lineLen = input.int(90, "水平線長度（K）", minval = 10)

//==================== EMA ====================
emaFast = ta.ema(close, emaFastLen)
emaMid  = ta.ema(close, emaMidLen)
emaSlow = ta.ema(close, emaSlowLen)

plot(emaFast, color=color.orange)
plot(emaMid,  color=color.gray)
plot(emaSlow, color=color.blue)

//==================== 趨勢 ====================
bullTrend = emaFast > emaMid and emaMid > emaSlow
bearTrend = emaFast < emaMid and emaMid < emaSlow

//==================== 回踩 ====================
bullPullback = low <= emaMid and low > emaSlow
bearPullback = high >= emaMid and high < emaSlow

//==================== 吞沒 ====================
bullEngulf = (
    close > open and
    close[1] < open[1] and
    close >= open[1] and
    open <= close[1]
)

bearEngulf = (
    close < open and
    close[1] > open[1] and
    open >= close[1] and
    close <= open[1]
)

//==================== 訊號 ====================
longSignal  = bullTrend and bullPullback and bullEngulf
shortSignal = bearTrend and bearPullback and bearEngulf

//==================== 物件（只保留最新一組） ====================
var line  entryLine = na
var line  slLine    = na
var line  tp1Line   = na
var line  tp2Line   = na

var label entryLab  = na
var label slLab     = na
var label tp1Lab    = na
var label tp2Lab    = na

f_clear() =>
    if not na(entryLine)
        line.delete(entryLine)
        line.delete(slLine)
        line.delete(tp1Line)
        line.delete(tp2Line)
        label.delete(entryLab)
        label.delete(slLab)
        label.delete(tp1Lab)
        label.delete(tp2Lab)

//==================== 多單 ====================
if longSignal
    f_clear()

    entry = close
    sl    = emaSlow
    risk  = entry - sl

    tp1 = entry + risk * 1.0
    tp2 = entry + risk * 1.5

    strategy.entry("Long", strategy.long)

    // 分批出場
    strategy.exit("Long TP1", "Long", limit = tp1, stop = sl, qty_percent = 50)
    strategy.exit("Long TP2", "Long", limit = tp2, stop = sl, qty_percent = 50)

    // 畫線
    entryLine := line.new(bar_index, entry, bar_index + lineLen, entry, color=color.white, width=2)
    slLine    := line.new(bar_index, sl,    bar_index + lineLen, sl,    color=color.red,   width=2)
    tp1Line   := line.new(bar_index, tp1,   bar_index + lineLen, tp1,   color=color.green, width=2)
    tp2Line   := line.new(bar_index, tp2,   bar_index + lineLen, tp2,   color=color.teal,  width=2)

    // 標籤（在三角形左邊）
    entryLab := label.new(bar_index - 1, entry, "Entry\n" + str.tostring(entry),
        style=label.style_label_right, textcolor=color.white, color=color.black)

    slLab := label.new(bar_index - 1, sl, "SL\n" + str.tostring(sl),
        style=label.style_label_right, textcolor=color.white, color=color.red)

    tp1Lab := label.new(bar_index - 1, tp1, "TP1 1:1\n" + str.tostring(tp1),
        style=label.style_label_right, textcolor=color.white, color=color.green)

    tp2Lab := label.new(bar_index - 1, tp2, "TP2 1:1.5\n" + str.tostring(tp2),
        style=label.style_label_right, textcolor=color.white, color=color.teal)

//==================== 空單 ====================
if shortSignal
    f_clear()

    entry = close
    sl    = emaSlow
    risk  = sl - entry

    tp1 = entry - risk * 1.0
    tp2 = entry - risk * 1.5

    strategy.entry("Short", strategy.short)

    // 分批出場
    strategy.exit("Short TP1", "Short", limit = tp1, stop = sl, qty_percent = 50)
    strategy.exit("Short TP2", "Short", limit = tp2, stop = sl, qty_percent = 50)

    // 畫線
    entryLine := line.new(bar_index, entry, bar_index + lineLen, entry, color=color.white, width=2)
    slLine    := line.new(bar_index, sl,    bar_index + lineLen, sl,    color=color.red,   width=2)
    tp1Line   := line.new(bar_index, tp1,   bar_index + lineLen, tp1,   color=color.green, width=2)
    tp2Line   := line.new(bar_index, tp2,   bar_index + lineLen, tp2,   color=color.teal,  width=2)

    // 標籤
    entryLab := label.new(bar_index - 1, entry, "Entry\n" + str.tostring(entry),
        style=label.style_label_right, textcolor=color.white, color=color.black)

    slLab := label.new(bar_index - 1, sl, "SL\n" + str.tostring(sl),
        style=label.style_label_right, textcolor=color.white, color=color.red)

    tp1Lab := label.new(bar_index - 1, tp1, "TP1 1:1\n" + str.tostring(tp1),
        style=label.style_label_right, textcolor=color.white, color=color.green)

    tp2Lab := label.new(bar_index - 1, tp2, "TP2 1:1.5\n" + str.tostring(tp2),
        style=label.style_label_right, textcolor=color.white, color=color.teal)

//==================== 三角形 ====================
plotshape(longSignal,  style=shape.triangleup,   location=location.belowbar, color=color.green, size=size.small)
plotshape(shortSignal, style=shape.triangledown, location=location.abovebar, color=color.red,   size=size.small)
