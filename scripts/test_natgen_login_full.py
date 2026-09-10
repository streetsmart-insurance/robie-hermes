import asyncio
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor

async def run():
    print('Starting Autonomous NatGen Login + 2FA Interception...')
    interceptor = MultiInboxOTPInterceptor(inboxes=['carlo@streetsmart.insurance', 'robie@streetsmart.insurance'])
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to NatGen login...')
            await page.goto('https://natgenagency.com/Login.aspx', wait_until='domcontentloaded')
            await asyncio.sleep(2)
            
            # Step 1: User ID
            await page.locator('input:visible, input[type=text]:visible').first.fill('Carlof')
            await page.locator('button:visible, a:visible, input[type=submit]:visible').filter(has_text='SIGN IN').first.click()
            await asyncio.sleep(4)
            
            # Step 2: Password
            await page.locator('input[type=password]:visible').first.fill('moxry8-dihzyw-Bognec')
            await page.locator('button:visible, input[type=submit]:visible, a:visible').filter(has_text='SIGN IN').first.click()
            await asyncio.sleep(5)
            
            print('2FA Selection URL:', page.url)
            # Step 3: Trigger Email OTP
            email_option = page.locator('#loginWith2faEmail')
            if await email_option.count() == 0:
                print('Could not find #loginWith2faEmail!')
                await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_2fa_missing.png')
                return
            
            trigger_time = int(time.time())
            print('Clicking #loginWith2faEmail at timestamp', trigger_time)
            await email_option.click()
            await asyncio.sleep(4)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_code_input_screen.png')
            print('Code Input Page URL:', page.url)
            
            # Step 4: Intercept OTP
            print('Waiting for OTP email in carlo@streetsmart.insurance...')
            otp_result = None
            for attempt in range(20):
                await asyncio.sleep(3)
                otp_result = interceptor.check_inbox_since(
                    inbox='carlo@streetsmart.insurance',
                    query='from:nationalgeneral OR subject:verification OR subject:code OR natgen',
                    since_timestamp=trigger_time - 10
                )
                if otp_result and otp_result.code:
                    print(f'Intercepted OTP Code: {otp_result.code} from {otp_result.sender} (Subject: {otp_result.subject})')
                    break
                else:
                    print(f'  Attempt {attempt+1}: waiting for code...')
                    
            if not otp_result or not otp_result.code:
                print('Failed to intercept OTP code within timeout.')
                return
                
            # Step 5: Fill OTP code
            print(f'Entering OTP code {otp_result.code} into form...')
            code_input = page.locator('input[type=text]:visible, input[name*=code i]:visible, input[id*=code i]:visible').first
            await code_input.fill(otp_result.code)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_code_filled.png')
            
            # Submit OTP
            submit_code_btn = page.locator('button:visible, input[type=submit]:visible, a:visible').filter(has_text='SUBMIT').first
            if await submit_code_btn.count() == 0:
                submit_code_btn = page.locator('button:visible, input[type=submit]:visible, a:visible').filter(has_text='SIGN IN').first
                
            print('Clicking submit OTP...')
            await submit_code_btn.click()
            await asyncio.sleep(8)
            
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_portal_dashboard.png')
            print('Post-2FA Dashboard URL:', page.url)
            
        finally:
            await page.close()

asyncio.run(run())
