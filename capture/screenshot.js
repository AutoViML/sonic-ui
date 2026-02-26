const puppeteer = require('puppeteer');
(async () => {
    try {
        const browser = await puppeteer.launch();
        const page = await browser.newPage();
        await page.setViewport({ width: 1280, height: 800 });
        await page.goto('http://localhost:9000', { waitUntil: 'networkidle0' });
        await page.screenshot({ path: '/Users/ramseshadri/syncra/docs/syncra-screenshot.png' });
        await browser.close();
        console.log('Screenshot saved');
    } catch (e) {
        console.error('Error taking screenshot:', e);
        process.exit(1);
    }
})();
