const { chromium } = require('../e2e/node_modules/playwright');
const path = require('path');
const { pathToFileURL } = require('url');

(async () => {
  const root = path.resolve(__dirname, '..');
  const html = path.join(root, 'docs', 'langfuse-observability-architecture(1).html');
  const pdf = path.join(root, 'docs', 'langfuse-observability-architecture(1).pdf');

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(pathToFileURL(html).href, { waitUntil: 'networkidle' });
  await page.addStyleTag({
    content: `
      body {
        font-family: "Microsoft YaHei", "SimSun", Arial, sans-serif;
        line-height: 1.55;
        color: #111;
        margin: 0 auto;
        max-width: 980px;
        padding: 28px 36px;
      }
      h1 {
        font-size: 26px;
        border-bottom: 2px solid #333;
        padding-bottom: 10px;
      }
      h2 {
        font-size: 20px;
        margin-top: 28px;
        border-bottom: 1px solid #ddd;
        padding-bottom: 6px;
      }
      h3 { font-size: 16px; margin-top: 20px; }
      code {
        font-family: Consolas, "Courier New", monospace;
        background: #f3f4f6;
        padding: 1px 4px;
        border-radius: 3px;
      }
      pre {
        font-family: Consolas, "Courier New", monospace;
        background: #f7f8fa;
        border: 1px solid #d6d6d6;
        border-radius: 6px;
        padding: 12px;
        white-space: pre-wrap;
        word-break: break-word;
        font-size: 10px;
        line-height: 1.25;
        overflow: visible;
      }
      table {
        border-collapse: collapse;
        width: 100%;
        font-size: 12px;
      }
      th, td {
        border: 1px solid #d6d6d6;
        padding: 6px 8px;
        vertical-align: top;
      }
      th { background: #f3f4f6; }
      blockquote {
        border-left: 4px solid #c00000;
        margin-left: 0;
        padding-left: 12px;
        color: #555;
      }
      a { color: #111; text-decoration: none; }
      .toc ul { list-style-type: none; padding-left: 16px; }
      @page { margin: 16mm; }
    `,
  });
  await page.pdf({
    path: pdf,
    format: 'A4',
    printBackground: true,
    margin: {
      top: '16mm',
      right: '14mm',
      bottom: '16mm',
      left: '14mm',
    },
  });
  await browser.close();
  console.log(pdf);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
