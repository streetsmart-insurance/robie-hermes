import asyncio
from playwright.async_api import async_playwright

async def execute_move():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        pg = b.contexts[0].pages[3]
        
        move_frames = [f for f in pg.frames if "MoveDocument" in f.url]
        if not move_frames:
            print("No MoveDocument frame found")
            return
        frame = move_frames[0]
        js_code = """
        () => {
            window.jQuery('#docTree').jstree('uncheck_all');
            window.jQuery('#docTree').jstree('check_node', '#578586724');
            const checked = window.jQuery('#docTree').jstree('get_checked', null, true);
            console.log('Checked:', checked.length);
            window.submitForm();
            return {checkedCount: checked.length};
        }
        """
        res = await frame.evaluate(js_code)
        print("Move initiated:", res)
        await asyncio.sleep(4)
        print("Done waiting!")

if __name__ == "__main__":
    asyncio.run(execute_move())
