import asyncio
import json
import os
import aiohttp
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

# 主菜单（保留数字前缀）
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

# --- 精确MA计算逻辑 ---
async def get_klines(symbol, market_type):
    interval = "15m"
    limit = 20
    if market_type == "contract":
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    else:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

def calculate_ma(klines):
    closes = [float(k[4]) for k in klines]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    current_price = closes[-1]
    return ma5, ma20, current_price

# --- 监控任务（核心修改部分）---
async def monitor_task(app):
    ma_history = {}  # 存储各币种历史MA值: {symbol_type: {'ma5': [], 'ma20': []}}
    
    while data["monitor"]:
        current_ma_values = {}
        
        # 第一步：获取所有币种最新MA值
        for item in data["symbols"]:
            symbol_key = f"{item['symbol']}_{item['type']}"
            try:
                klines = await get_klines(item["symbol"], item["type"])
                if klines and len(klines) >= 20:  # 确保有足够数据
                    ma5, ma20, price = calculate_ma(klines)
                    current_ma_values[symbol_key] = {
                        'ma5': ma5,
                        'ma20': ma20,
                        'price': price
                    }
                    
                    # 初始化历史记录
                    if symbol_key not in ma_history:
                        ma_history[symbol_key] = {'ma5': [], 'ma20': []}
                    
                    # 保留最近3个值用于确认趋势
                    ma_history[symbol_key]['ma5'].append(ma5)
                    ma_history[symbol_key]['ma20'].append(ma20)
                    if len(ma_history[symbol_key]['ma5']) > 3:
                        ma_history[symbol_key]['ma5'].pop(0)
                        ma_history[symbol_key]['ma20'].pop(0)
                
            except Exception as e:
                print(f"获取 {item['symbol']} 数据出错: {e}")
                continue
        
        # 第二步：检测交叉信号
        for symbol_key, values in current_ma_values.items():
            if symbol_key not in ma_history or len(ma_history[symbol_key]['ma5']) < 2:
                continue
                
            # 获取当前和前值
            prev_ma5 = ma_history[symbol_key]['ma5'][-2]
            prev_ma20 = ma_history[symbol_key]['ma20'][-2]
            curr_ma5 = values['ma5']
            curr_ma20 = values['ma20']
            price = values['price']
            
            # 上穿检测（金叉）
            if prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20:
                signal = (
                    f"📈 买入信号 {symbol_key.replace('_', ' ')}\n"
                    f"价格: {price:.4f}\n"
                    f"MA5: {curr_ma5:.4f} (前值 {prev_ma5:.4f})\n"
                    f"MA20: {curr_ma20:.4f} (前值 {prev_ma20:.4f})"
                )
                # 发送给所有活跃用户
                for uid in user_states.keys():
                    try:
                        await app.bot.send_message(chat_id=uid, text=signal)
                    except Exception as e:
                        print(f"发送消息给 {uid} 失败: {e}")
            
            # 下穿检测（死叉）
            elif prev_ma5 >= prev_ma20 and curr_ma5 < curr_ma20:
                signal = (
                    f"📉 卖出信号 {symbol_key.replace('_', ' ')}\n"
                    f"价格: {price:.4f}\n"
                    f"MA5: {curr_ma5:.4f} (前值 {prev_ma5:.4f})\n"
                    f"MA20: {curr_ma20:.4f} (前值 {prev_ma20:.4f})"
                )
                for uid in user_states.keys():
                    try:
                        await app.bot.send_message(chat_id=uid, text=signal)
                    except Exception as e:
                        print(f"发送消息给 {uid} 失败: {e}")
        
        # 严格每分钟执行一次
        await asyncio.sleep(60)

# --- 以下保持原有代码不变 ---
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
                    _, _, price = calculate_ma(await get_klines(s["symbol"], s["type"]))
                    msg += f"{s['symbol']} ({s['type']}): {price}\n"
                except:
                    msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
            
            await query.message.reply_text(msg, reply_markup=reply_markup)
        else:
            await query.message.reply_text("您可以在菜单中手动开启监控", reply_markup=reply_markup)

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

async def start(update, context):
    user_states[update.effective_chat.id] = {}
    await update.message.reply_text("欢迎使用 MA 监控机器人", reply_markup=reply_markup)

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

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(CallbackQueryHandler(button_callback))

print("机器人已启动")
app.run_polling()
