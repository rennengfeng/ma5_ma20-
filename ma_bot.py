import asyncio
import json
import os
import aiohttp
import time
from datetime import datetime
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from config import TOKEN, CHAT_ID

DATA_FILE = "symbols.json"

# K线参数配置
INTERVAL = "15m"      # 15分钟K线
MA5_PERIOD = 9        # 改为MA9
MA20_PERIOD = 26      # 改为MA26

# 主菜单
main_menu = [
    ["1. 添加币种", "2. 删除币种"],
    ["3. 开启监控", "4. 停止监控"],
    ["5. 查看状态", "6. 帮助"]
]
reply_markup = ReplyKeyboardMarkup(main_menu, resize_keyboard=True)

# --- 数据管理 ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            try:
                data = json.load(f)
                if "symbols" in data:
                    new_symbols = []
                    for s in data["symbols"]:
                        if isinstance(s, str):
                            new_symbols.append({"symbol": s, "type": "spot"})
                        else:
                            new_symbols.append(s)
                    data["symbols"] = new_symbols
                else:
                    data = {"symbols": [], "monitor": False}
                return data
            except:
                return {"symbols": [], "monitor": False}
    return {"symbols": [], "monitor": False}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = load_data()
monitoring_task = None
user_states = {}
prev_klines = {}  # 缓存各币种上一次的K线数据

# --- MA计算函数 ---
async def get_klines(symbol, market_type):
    limit = max(MA5_PERIOD, MA20_PERIOD) + 5  # 多取几根防止边界问题
    if market_type == "contract":
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval={INTERVAL}&limit={limit}"
    else:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={INTERVAL}&limit={limit}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                klines = await resp.json()
                print(f"{symbol} 最新K线时间: {datetime.fromtimestamp(klines[-1][0]/1000)}")
                return klines
            return None

def calculate_ma(klines):
    closes = [float(k[4]) for k in klines]  # 收盘价列表
    ma9 = sum(closes[-MA5_PERIOD:]) / MA5_PERIOD
    ma26 = sum(closes[-MA20_PERIOD:]) / MA20_PERIOD
    current_price = closes[-1]
    return ma9, ma26, current_price

# --- 监控任务（方案二实现）---
async def monitor_task(app):
    prev_states = {}  # 保存各币种上次的MA值
    
    while data["monitor"]:
        for item in data["symbols"]:
            symbol_key = f"{item['symbol']}_{item['type']}"
            try:
                # 获取最新K线数据
                klines = await get_klines(item["symbol"], item["type"])
                if not klines or len(klines) < MA20_PERIOD:
                    continue
                
                # 检查是否是新K线（对比开盘时间）
                if symbol_key in prev_klines:
                    last_kline_time = prev_klines[symbol_key][-1][0]
                    if klines[-1][0] == last_kline_time:
                        continue  # K线未更新，跳过计算
                
                # K线更新，计算MA值
                ma9, ma26, price = calculate_ma(klines)
                prev_klines[symbol_key] = klines  # 更新缓存
                
                # 信号检测（需有历史数据）
                if symbol_key in prev_states:
                    prev_ma9, prev_ma26 = prev_states[symbol_key]
                    
                    # 上穿：MA9从下方穿过MA26
                    if prev_ma9 <= prev_ma26 and ma9 > ma26:
                        signal = (
                            f"📈 买入信号 {item['symbol']} ({item['type']})\n"
                            f"价格: {price:.4f}\n"
                            f"MA9: {ma9:.4f} (前值 {prev_ma9:.4f})\n"
                            f"MA26: {ma26:.4f} (前值 {prev_ma26:.4f})"
                        )
                        for uid in user_states.keys():
                            await app.bot.send_message(chat_id=uid, text=signal)
                    
                    # 下穿：MA9从上方穿过MA26
                    elif prev_ma9 >= prev_ma26 and ma9 < ma26:
                        signal = (
                            f"📉 卖出信号 {item['symbol']} ({item['type']})\n"
                            f"价格: {price:.4f}\n"
                            f"MA9: {ma9:.4f} (前值 {prev_ma9:.4f})\n"
                            f"MA26: {ma26:.4f} (前值 {prev_ma26:.4f})"
                        )
                        for uid in user_states.keys():
                            await app.bot.send_message(chat_id=uid, text=signal)
                
                # 保存当前MA值
                prev_states[symbol_key] = (ma9, ma26)
                
            except Exception as e:
                print(f"监控 {item['symbol']} 出错: {e}")
        
        # 每分钟检查一次（实际计算仅在K线更新时触发）
        await asyncio.sleep(60)

# --- 删除后刷新列表 ---
async def refresh_delete_list(update, user_id):
    if not data["symbols"]:
        await update.message.reply_text("已无更多币种可删除", reply_markup=reply_markup)
        user_states[user_id] = {}
        return
    
    msg = "请选择要删除的币种：\n"
    for idx, s in enumerate(data["symbols"], 1):
        msg += f"{idx}. {s['symbol']} ({s['type']})\n"
    
    user_states[user_id] = {"step": "delete_symbol"}
    await update.message.reply_text(msg + "\n请输入编号继续删除，或输入0返回", reply_markup=reply_markup)

# --- 按钮回调 ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data_parts = query.data.split(":")

    if data_parts[0] == "select_type":
        symbol = data_parts[1]
        market_type = data_parts[2]
        data["symbols"].append({"symbol": symbol, "type": market_type})
        save_data(data)
        await query.edit_message_text(f"已添加 {symbol} ({market_type})")
        
        keyboard = [
            [InlineKeyboardButton("是", callback_data="continue_add:yes")],
            [InlineKeyboardButton("否", callback_data="continue_add:no")]
        ]
        await query.message.reply_text(
            "是否继续添加币种？",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data_parts[0] == "continue_add":
        if data_parts[1] == "yes":
            user_states[user_id] = {"step": "add_symbol"}
            await query.message.reply_text("请输入币种（如 BTCUSDT）：输入0取消", reply_markup=reply_markup)
        else:
            user_states[user_id] = {}
            keyboard = [
                [InlineKeyboardButton("是", callback_data="start_monitor:yes")],
                [InlineKeyboardButton("否", callback_data="start_monitor:no")]
            ]
            await query.message.reply_text(
                "是否立即开启监控？",
                reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data_parts[0] == "start_monitor":
        if data_parts[1] == "yes":
            data["monitor"] = True
            save_data(data)
            global monitoring_task
            if not monitoring_task:
                monitoring_task = asyncio.create_task(monitor_task(context.application))
            
            msg = "监控已开启\n当前监控列表：\n"
            for s in data["symbols"]:
                try:
                    klines = await get_klines(s["symbol"], s["type"])
                    if klines:
                        _, _, price = calculate_ma(klines)
                        msg += f"{s['symbol']} ({s['type']}): {price}\n"
                    else:
                        msg += f"{s['symbol']} ({s['type']}): 获取数据失败\n"
                except:
                    msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
            
            await query.message.reply_text(msg, reply_markup=reply_markup)
        else:
            await query.message.reply_text("您可以在菜单中手动开启监控", reply_markup=reply_markup)

# --- 启动命令 ---
async def start(update, context):
    user_states[update.effective_chat.id] = {}
    await update.message.reply_text("欢迎使用 MA 监控机器人", reply_markup=reply_markup)

# --- 消息处理 ---
async def handle_message(update, context):
    user_id = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in ["0", "no"]:
        user_states[user_id] = {}
        await update.message.reply_text("操作已取消", reply_markup=reply_markup)
        return

    state = user_states.get(user_id, {})
    
    if state.get("step") == "delete_symbol":
        try:
            idx = int(text) - 1
            if 0 <= idx < len(data["symbols"]):
                removed = data["symbols"].pop(idx)
                save_data(data)
                await update.message.reply_text(f"已删除 {removed['symbol']}")
                await refresh_delete_list(update, user_id)
            else:
                await update.message.reply_text("编号无效，请重新输入", reply_markup=reply_markup)
        except ValueError:
            await update.message.reply_text("请输入数字编号", reply_markup=reply_markup)
        return

    command = text.split(".")[0].strip() if "." in text else text
    command = command.split()[0].strip()

    if command == "1" or "添加币种" in text:
        user_states[user_id] = {"step": "add_symbol"}
        await update.message.reply_text("请输入币种（如 BTCUSDT）：输入0取消", reply_markup=reply_markup)
        return
    elif command == "2" or "删除币种" in text:
        if not data["symbols"]:
            await update.message.reply_text("当前无已添加币种", reply_markup=reply_markup)
            return
        
        msg = "请选择要删除的币种：\n"
        for idx, s in enumerate(data["symbols"], 1):
            msg += f"{idx}. {s['symbol']} ({s['type']})\n"
        
        user_states[user_id] = {"step": "delete_symbol"}
        await update.message.reply_text(msg + "\n请输入编号删除，或输入0取消", reply_markup=reply_markup)
        return
    elif command == "3" or "开启监控" in text:
        data["monitor"] = True
        save_data(data)
        global monitoring_task
        if not monitoring_task:
            monitoring_task = asyncio.create_task(monitor_task(context.application))
        
        msg = "监控已开启\n当前监控列表：\n"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"{s['symbol']} ({s['type']}): {price}\n"
                else:
                    msg += f"{s['symbol']} ({s['type']}): 获取数据失败\n"
            except:
                msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
        
        await update.message.reply_text(msg, reply_markup=reply_markup)
        return
    elif command == "4" or "停止监控" in text:
        data["monitor"] = False
        save_data(data)
        await update.message.reply_text("监控已停止", reply_markup=reply_markup)
        return
    elif command == "5" or "查看状态" in text:
        if not data["symbols"]:
            await update.message.reply_text("暂无监控币种", reply_markup=reply_markup)
        else:
            msg = "当前监控币种：\n"
            for s in data["symbols"]:
                msg += f"{s['symbol']} ({s['type']})\n"
            msg += f"监控状态: {'开启' if data['monitor'] else '关闭'}"
            await update.message.reply_text(msg, reply_markup=reply_markup)
        return
    elif command == "6" or "帮助" in text:
        help_text = (
            "功能说明：\n"
            "1. 添加币种\n"
            "2. 删除币种\n"
            "3. 开启监控\n"
            "4. 停止监控\n"
            "5. 查看状态\n"
            "6. 帮助\n"
            "输入 0 或 no 可取消当前操作"
        )
        await update.message.reply_text(help_text, reply_markup=reply_markup)
        return

    if state.get("step") == "add_symbol":
        keyboard = [
            [InlineKeyboardButton("现货", callback_data=f"select_type:{text.upper()}:spot")],
            [InlineKeyboardButton("合约", callback_data=f"select_type:{text.upper()}:contract")]
        ]
        await update.message.reply_text(
            f"请选择 {text.upper()} 的类型：",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# --- 主程序 ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("机器人已启动（MA9/MA26监控）")
    app.run_polling()
