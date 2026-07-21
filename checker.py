import json
import asyncio
from playwright.async_api import async_playwright
from fake_useragent import UserAgent
import time
import random
from typing import Dict, Optional, List
import os

# ==================== 配置区域 ====================
# 搜索配置
SEARCH_APP_ID = "932747118"  # 要搜索的App ID

# 代理配置
PROXY_LIST = [
    # {"server": "http://127.0.0.1:7890"},
    # {"server": "socks5://127.0.0.1:1080", "username": "user", "password": "pass"},
]
MAX_CONCURRENT = 3 if PROXY_LIST else 1  # 并发数

# 文件配置
INPUT_FILE = "accounts.json"
OUTPUT_FILE = "accounts_checked.json"
TEMP_OUTPUT_FILE = "accounts_checked_temp.json"
CONFIG_FILE = "config.json"

# 浏览器配置
HEADLESS = True  # 是否无头模式

# 延迟配置
MIN_DELAY = 5  # 最小延迟（秒）
MAX_DELAY = 10  # 最大延迟（秒）

# ==================== 全局状态 ====================
results = {}  # 存储处理结果
results_lock = asyncio.Lock()  # 结果写入锁
proxy_index = 0  # 代理轮询索引
proxy_lock = asyncio.Lock()  # 代理获取锁

# ==================== 辅助函数 ====================


def load_config():
    """从配置文件加载配置"""
    if not os.path.exists(CONFIG_FILE):
        # 生成配置模板
        template = {
            "SEARCH_APP_ID": SEARCH_APP_ID,
            "MAX_CONCURRENT": MAX_CONCURRENT,
            "PROXY_LIST": PROXY_LIST,
            "HEADLESS": HEADLESS,
            "MIN_DELAY": MIN_DELAY,
            "MAX_DELAY": MAX_DELAY,
            "INPUT_FILE": INPUT_FILE,
            "OUTPUT_FILE": OUTPUT_FILE
        }
        with open("config_template.json", 'w', encoding='utf-8') as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        print(f"💡 提示：可以编辑 config_template.json 并重命名为 {CONFIG_FILE}")
        return

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 更新全局变量
            globals().update(
                {k: v for k, v in config.items() if k in globals()})
            print(f"✅ 已从 {CONFIG_FILE} 加载配置")
    except Exception as e:
        print(f"⚠️ 加载配置文件失败: {e}")


def load_existing_results():
    """加载已存在的结果"""
    global results
    for file in [TEMP_OUTPUT_FILE, OUTPUT_FILE]:
        if not os.path.exists(file):
            continue
        try:
            with open(file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    results = {
                        item['id']: item for item in data if 'id' in item}
                    print(f"📂 已加载 {len(results)} 个已处理结果从 {file}")
                    return
        except Exception as e:
            print(f"⚠️ 加载现有结果失败: {e}")


async def save_result(account: Dict):
    """保存单个结果并立即写入文件"""
    async with results_lock:
        results[account['id']] = account
        try:
            with open(TEMP_OUTPUT_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(results.values()), f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存临时结果失败: {e}")


def finalize_results(original_accounts: List[Dict]):
    """最终保存，按原始顺序排序"""
    try:
        sorted_results = []
        for account in original_accounts:
            if account['id'] in results:
                sorted_results.append(results[account['id']])
            else:
                unprocessed = account.copy()
                unprocessed['check'] = "⏭️未处理"
                sorted_results.append(unprocessed)

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(sorted_results, f, ensure_ascii=False, indent=2)

        if os.path.exists(TEMP_OUTPUT_FILE):
            os.remove(TEMP_OUTPUT_FILE)

        return sorted_results
    except Exception as e:
        print(f"⚠️ 最终保存失败: {e}")
        return list(results.values())


async def get_proxy() -> Optional[Dict]:
    """轮询获取代理"""
    global proxy_index
    if not PROXY_LIST:
        return None

    async with proxy_lock:
        proxy = PROXY_LIST[proxy_index]
        proxy_index = (proxy_index + 1) % len(PROXY_LIST)
        return proxy

# ==================== 核心功能函数 ====================


async def login(page, id: str, password: str):
    """执行完整登录流程"""
    try:
        # 等待登录iframe
        iframe_locator = page.locator('iframe#aid-auth-widget-iFrame')
        await iframe_locator.wait_for()
        frame_locator = iframe_locator.content_frame

        # 输入用户名
        await frame_locator.locator('#account_name_text_field').fill(id)
        time.sleep(random.uniform(1, MIN_DELAY))
        await frame_locator.locator('button#sign-in').click()

        # 等待下一步
        continue_button = frame_locator.locator('button#continue-password')
        password_field = frame_locator.locator(
            'input#password_text_field:not([tabindex="-1"])')

        task_continue = asyncio.create_task(
            continue_button.wait_for(state='visible'))
        task_password = asyncio.create_task(
            password_field.wait_for(state='visible'))

        done, pending = await asyncio.wait([task_continue, task_password], return_when=asyncio.FIRST_COMPLETED)
        [task.cancel() for task in pending]

        if task_continue in done:
            await continue_button.click()
            await password_field.wait_for(state='visible')

        # 输入密码并登录
        await password_field.fill(password)
        await frame_locator.locator('button#sign-in').click()

        # 检查登录结果
        checks = [
            ('.idms-error', 'error_login', False),
            ('#errMsg', 'error_login', False),
            ('iframe#account-repair-widget-iFrame', 'repair_iframe', False),
            ('div.verify-phone', 'phone_verification', False),
            ('div.verify-device', 'device_verification', False),
            ('div#acc-locked', 'account_locked', False),
            ('.app', 'purchase_page', True),  # True表示在page上查找
        ]

        async def check_element(selector, status, on_page):
            try:
                locator = (page if on_page else frame_locator).locator(
                    selector)
                await locator.wait_for()
                return status
            except:
                return None

        for future in asyncio.as_completed([check_element(*check) for check in checks]):
            result = await future
            if result:
                status = result
                break
        else:
            status = None

        # 处理登录结果
        if status == "purchase_page":
            return True
        elif status == "repair_iframe":
            repairFrame = frame_locator.frame_locator(
                'iframe#account-repair-widget-iFrame')
            await repairFrame.locator('button#other-options-button').click()
            await repairFrame.locator('button#dont-upgrade-button').click()
            return True
        elif status == "error_login":
            for selector in ['.idms-error', '#errMsg']:
                try:
                    error_element = frame_locator.locator(selector)
                    if await error_element.count() > 0:
                        error_text = await error_element.first.inner_text()
                        return f"错误提示: {error_text}"
                except:
                    continue
            return "错误提示: 未知错误"
        elif status == "phone_verification":
            return "需要进行电话验证，请处理。"
        elif status == "device_verification":
            return "需要进行设备验证，请处理。"
        elif status == "account_locked":
            return "账号被锁定，请处理。"
        else:
            return "啥也没命中"

    except Exception as e:
        return f"登录出错：{e}"


async def process_account(playwright, account: Dict) -> Dict:
    """处理单个账号"""
    browser = None
    start_time = time.time()
    max_retries = 2
    app_id = account.get('search_app', SEARCH_APP_ID)

    for attempt in range(max_retries):
        try:
            # 获取代理
            proxy = await get_proxy() if PROXY_LIST and MAX_CONCURRENT > 1 else None
            if proxy:
                print(
                    f"账号 {account['id']} 使用代理: {proxy.get('server', 'unknown')}")

            # 启动浏览器
            launch_options = {
                "headless": HEADLESS,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                    "--window-size=1280,800"
                ]
            }
            if proxy:
                launch_options["proxy"] = proxy

            browser = await playwright.chromium.launch(**launch_options)
            context = await browser.new_context(
                bypass_csp=True,
                user_agent=UserAgent().random,
                viewport={"width": 1280, "height": 800},
            )

            # 反检测脚本注入
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            page = await context.new_page()

            # 使用事件来确保登录信息被捕获
            login_info = {'x_apple_rap2_api': None,
                          'token': None, 'dsid': None}
            login_captured = asyncio.Event()

            async def on_route(route):
                """捕获登录请求的headers"""
                if "/api/login" in route.request.url and route.request.method == "GET":
                    login_info['x_apple_rap2_api'] = route.request.headers.get(
                        "x-apple-rap2-api")
                await route.continue_()

            async def on_response(response):
                """捕获登录响应"""
                if "/api/login" in response.url and response.status == 200:
                    try:
                        data = await response.json()
                        login_info['token'] = data.get("token")
                        login_info['dsid'] = data.get("dsid")
                        # 设置事件标志，表示已捕获到登录信息
                        if all(login_info.values()):
                            login_captured.set()
                    except:
                        pass

            # 设置路由和响应监听
            await page.route("**/api/login", on_route)
            page.on("response", on_response)

            await page.goto("https://reportaproblem.apple.com/", wait_until="domcontentloaded")

            # 登录
            login_result = await login(page, account['id'], account['password'])

            # 处理身份验证错误
            if "无法验证你的身份" in str(login_result) and attempt < max_retries - 1:
                print(f"账号 {account['id']} 遇到身份验证错误，重试中...")
                await browser.close()
                browser = None
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            if login_result != True:
                account['check'] = f"❗登录失败: {login_result}"
                break

            print(f"账号 {account['id']} 登录成功，等待获取认证信息...")

            # 第一阶段：等待登录信息或错误
            task_login_captured = asyncio.create_task(login_captured.wait())
            task_error = asyncio.create_task(page.locator(
                ".error-content").wait_for(state='visible'))

            done, pending = await asyncio.wait([task_login_captured, task_error], return_when=asyncio.FIRST_COMPLETED, timeout=15)

            for task in pending:
                task.cancel()

            # 处理第一阶段结果
            if not done:  # 超时
                account['check'] = f"❌获取登录认证信息超时"
                break
            elif task_error in done:
                error_text = await page.locator('.error-content').inner_text()
                account['check'] = f"❗页面错误: {error_text}"
                break
            elif task_login_captured in done:
                # 验证登录信息完整性
                if not all([login_info['x_apple_rap2_api'], login_info['token'], login_info['dsid']]):
                    missing = []
                    if not login_info['x_apple_rap2_api']:
                        missing.append('rap2_api')
                    if not login_info['token']:
                        missing.append('token')
                    if not login_info['dsid']:
                        missing.append('dsid')
                    account['check'] = f"❌登录信息不完整，缺少: {', '.join(missing)}"
                    break

                print(
                    f"\033[34mℹ️ 成功获取账号 {account['id']} 的认证信息，开始检索...\033[0m")

                # 第二阶段：查找App
                try:
                    target_info = await asyncio.wait_for(find_app(page, app_id, login_info), timeout=30)

                    if target_info:
                        account['check'] = True
                        account['details'] = target_info
                    else:
                        account['check'] = False

                except asyncio.TimeoutError:
                    account['check'] = f"❌检索软件超时"
                except Exception as e:
                    account['check'] = f"❌检索软件出错: {str(e)}"

            break

        except Exception as e:
            if attempt < max_retries - 1:
                print(f"❗账号 {account['id']} 处理失败 ({e})，重试中...")
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue
            account['check'] = f"❌处理失败: {str(e)}"

        finally:
            if browser:
                try:
                    await browser.close()
                except:
                    pass

    # 记录处理信息
    account['process_time'] = f"{time.time() - start_time:.2f}秒"
    account['timestamp'] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 保存结果
    await save_result(account)

    return account


async def find_app(page, app_id: str, login_info: dict):
    """使用预先捕获的登录信息查找App购买记录"""
    try:
        x_apple_rap2_api = login_info.get('x_apple_rap2_api')
        token = login_info.get('token')
        dsid = login_info.get('dsid')

        if not all([x_apple_rap2_api, token, dsid]):
            print(
                f"❌ 缺少必要的登录信息: rap2_api={bool(x_apple_rap2_api)}, token={bool(token)}, dsid={bool(dsid)}")
            return None

        # 在浏览器里发起搜索请求
        purchases = await page.evaluate(f"""
async (app_id) => {{
    const resp = await fetch("/api/purchase/search", {{
        method: "POST",
        headers: {{
            "Content-Type": "application/json",
            "X-Apple-Rap2-Api": "{x_apple_rap2_api}",
            "X-Apple-Xsrf-Token": "{token}"
        }},
        credentials: "include",
        body: JSON.stringify({{ adamIds: [app_id], dsid: "{dsid}" }})
    }});
    
    if (!resp.ok) {{
        const text = await resp.text();
        return {{ error: 'API fetch failed', status: resp.status, text: text }};
    }}
    
    const data = await resp.json();
    const purchases = data.purchases || [];
    return purchases.flatMap(p => (p.plis || []).map(pli => {{
        const c = pli.localizedContent || {{}};
        return {{
            app_name: c.nameForDisplay,
            publisher: c.detailForDisplay,
            price: pli.amountPaid
        }};
    }}));
}}
""", app_id)

        if isinstance(purchases, dict) and 'error' in purchases:
            print(f"❌ find_app API 请求失败: {purchases['error']}")
            return None

        if purchases and len(purchases) > 0:
            return purchases

        return None

    except Exception as e:
        print(f"❌ find_app 出错: {e}")
        return None


async def main():
    """主函数"""
    try:
        # 加载配置
        load_config()
        load_existing_results()

        # 读取账号
        with open(INPUT_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)

        # 过滤已处理账号
        processed_ids = set(results.keys())
        accounts_to_process = []

        for account in accounts:
            if 'search_app' not in account:
                account['search_app'] = SEARCH_APP_ID

            if (account['id'] not in processed_ids or (results.get(account['id'], {}).get('check') not in [False, True])):
                accounts_to_process.append(account)
            else:
                print(f"⏭️ 跳过已处理: {account['id']}")

        if not accounts_to_process:
            print("\033[32m✅ 所有账号都已处理完成！\033[0m")
            return

        # 显示运行信息
        print(f"\n\033[36m{'='*60}\033[0m")
        print("\033[36m🚀 Apple账号检查器\033[0m")
        print(f"\033[36m{'='*60}\033[0m")
        print(
            f"\033[36m📋 待处理: {len(accounts_to_process)}/{len(accounts)}\033[0m")
        print(f"\033[36m🔍 搜索软件: {SEARCH_APP_ID}\033[0m")
        print(
            f"\033[36m🌐 模式: {'代理' if PROXY_LIST and MAX_CONCURRENT > 1 else '直连'}\033[0m")
        print(f"\033[36m⚡ 并发数: {MAX_CONCURRENT}\033[0m")
        print(f"\033[36m{'='*60}\n\033[0m")

        async with async_playwright() as playwright:
            if PROXY_LIST and MAX_CONCURRENT > 1:
                # 并发处理
                semaphore = asyncio.Semaphore(MAX_CONCURRENT)

                async def process_with_limit(account):
                    async with semaphore:
                        return await process_account(playwright, account)

                tasks = [process_with_limit(acc)
                         for acc in accounts_to_process]
                for i, future in enumerate(asyncio.as_completed(tasks), 1):
                    result = await future
                    print(
                        f"\033[32m[{i}/{len(tasks)}] ✅ {result['id']} - {result['check']}\033[0m" if result.get('check') is True else
                        f"\033[31m[{i}/{len(tasks)}] ⛔ {result['id']} - {result['check']}\033[0m" if result.get('check') is False else
                        f"\033[33m[{i}/{len(tasks)}] ❗ {result['id']} - {result['check']}\033[0m"
                    )
            else:
                # 顺序处理
                for i, account in enumerate(accounts_to_process):
                    print(
                        f"\n\033[34m⏳ 处理 {i+1}/{len(accounts_to_process)}\033[0m")
                    result = await process_account(playwright, account)
                    check = result.get('check')
                    if check is True:
                        print(
                            f"\033[32m✅ {result['id']} - 已购买 ({result.get('process_time', 'N/A')})\033[0m")
                    elif check is False:
                        print(
                            f"\033[31m⛔ {result['id']} - 未购买 ({result.get('process_time', 'N/A')})\033[0m")
                    else:
                        print(
                            f"\033[33m❗ {result['id']} - {check} ({result.get('process_time', 'N/A')})\033[0m")

        # 保存最终结果
        final_results = finalize_results(accounts)

        # 统计
        stats = {
            '✔️ 成功': sum(1 for a in final_results if a.get('check') is True),
            '❌ 未找到': sum(1 for a in final_results if a.get('check') is False),
            '❗ 失败': sum(1 for a in final_results if isinstance(a.get('check'), str) and '❗' in a.get('check')),
            '⏭️ 未处理': sum(1 for a in final_results if isinstance(a.get('check'), str) and '⏭️' in a.get('check'))
        }

        print(f"\n\033[36m{'='*60}\033[0m")
        print(f"\033[32m✅ 处理完成！\033[0m")
        print(f"\033[36m📁 结果: {OUTPUT_FILE}\033[0m")
        print(f"\n\033[36m📊 统计:\033[0m")
        for key, value in stats.items():
            if value > 0:
                print(f"\033[36m  {key}: {value}\033[0m")
        print(f"\033[36m{'='*60}\n\033[0m")

    except FileNotFoundError:
        print(f"\033[31m❌ 找不到文件: {INPUT_FILE}\033[0m")
    except Exception as e:
        print(f"\033[31m❌ 程序错误: {str(e)}\033[0m")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
