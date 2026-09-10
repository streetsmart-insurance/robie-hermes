/**
 * StreetSmart Insurance — Clean Client Proposal Generator
 *
 * Usage: node generate_proposal.js data.json output.docx
 *
 * data.json schema:
 * {
 *   "business_name": "ATBR LLC (DBA: T & C EXPRESS)",
 *   "address": "723 S RIVER STREET, CALHOUN, GA 30701",
 *   "phone": "(770) 548-7165",
 *   "email": "garlandcook23@gmail.com",
 *   "business_type": "Trucking",
 *   "usdot_number": "1234567",
 *   "policy_period": "08/04/2026 to 08/04/2027",
 *   "rated_drivers": [
 *     {"name": "Justin Jones", "date_of_birth": "11/26/1979", "points": "0", "additional_information": ""}
 *   ],
 *   "radius_of_operation": "500 miles",
 *   "coverage_groups": [
 *     {
 *       "title": "Commercial Automobile Liability",
 *       "items": [
 *         {"name": "Bodily Injury / Property Damage", "limit": "$1,000,000 Combined Single Limit"},
 *         {"name": "Uninsured Motorists (UM)", "limit": "$75,000"}
 *       ]
 *     }
 *   ],
 *   "total_annual_premium": "$88,888.01",
 *   "required_initial_payment": "$18,115.65",
 *   "taxes_and_fees": "$0.00",                  // only when explicitly authorized by the user
 *   "required_initial_payment_includes_taxes_and_fees": false,
 *   "payment_plan_label": "10 monthly payments",
 *   "monthly_installment": "$7,773.83"
 * }
 */

const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  WidthType, ShadingType, AlignmentType, BorderStyle, HeadingLevel,
  ImageRun, Header, Footer, PageBreak, VerticalAlign, convertInchesToTwip,
  ExternalHyperlink
} = require("docx");

const ASSETS = path.join(__dirname, "assets");
const BLUE = "1330B4";
const YELLOW = "FFD500";
const SLATE = "3D5875";
const LIGHT_GRAY = "F2F2F2";

const PAGE_WIDTH = 12240; // US Letter, DXA
const PAGE_HEIGHT = 15840;
const MARGIN = convertInchesToTwip(0.75);
const CONTENT_WIDTH = PAGE_WIDTH - MARGIN * 2;

function parseMoney(amountStr) {
  const n = parseFloat(String(amountStr || "").replace(/[^0-9.\-]/g, ""));
  return Number.isFinite(n) ? n : 0;
}

function formatMoney(n) {
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function addMoney(baseAmount, additionalAmount) {
  return formatMoney(parseMoney(baseAmount) + parseMoney(additionalAmount));
}

function disclaimerFooter() {
  return new Footer({
    children: [
      new Paragraph({
        border: { top: { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC" } },
        spacing: { before: 100 },
        children: [
          new TextRun({
            text: "This is only a brief summary, not a contract. Please see full policy details for limitations and exclusions.",
            size: 14,
            color: "666666",
            italics: true
          })
        ]
      })
    ]
  });
}

function stripeHeader() {
  const stripePath = path.join(ASSETS, "stripe.png");
  if (!fs.existsSync(stripePath)) return new Header({ children: [new Paragraph("")] });
  return new Header({
    children: [
      new Paragraph({
        children: [
          new ImageRun({ type: "png", data: fs.readFileSync(stripePath), transformation: { width: 90, height: 65 } })
        ]
      })
    ]
  });
}

function h1(text) {
  return new Paragraph({
    spacing: { before: 240, after: 240 },
    children: [new TextRun({ text, bold: true, size: 40, color: BLUE })]
  });
}

function h2(text) {
  return new Paragraph({
    spacing: { before: 360, after: 200 },
    children: [new TextRun({ text, bold: true, size: 32, color: SLATE })]
  });
}

function bodyPara(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 160 },
    children: [new TextRun({ text, size: 26, ...opts })]
  });
}

function bullet(text, opts = {}) {
  return new Paragraph({
    bullet: { level: 0 },
    spacing: { after: 160 },
    children: [new TextRun({ text, size: 28, ...opts })]
  });
}

function subBullet(children) {
  return new Paragraph({
    bullet: { level: 1 },
    spacing: { after: 160 },
    children
  });
}

// Bullet with a bold, highlighted lead-in label followed by plain text — used to make
// key phrases pop (e.g. "Text Alerts:", "Client Center:") without highlighting the whole line.
function highlightBullet(label, rest) {
  return new Paragraph({
    bullet: { level: 0 },
    spacing: { after: 180 },
    children: [
      new TextRun({ text: label, bold: true, size: 30, highlight: "yellow" }),
      new TextRun({ text: rest, size: 30 })
    ]
  });
}

// Bullet in bold red — used for things that must not be missed (e.g. Binding Requirements).
function redBullet(text) {
  return new Paragraph({
    bullet: { level: 0 },
    spacing: { after: 160 },
    children: [new TextRun({ text, bold: true, size: 28, color: "C00000" })]
  });
}

function linkRun(text, url) {
  return new ExternalHyperlink({
    link: url,
    children: [new TextRun({ text, size: 28, bold: true, color: BLUE, underline: {} })]
  });
}

function labeledBullet(label, value) {
  return new Paragraph({
    bullet: { level: 0 },
    spacing: { after: 160 },
    children: [
      new TextRun({ text: `${label}: `, bold: true, size: 28 }),
      new TextRun({ text: value || "", size: 28 })
    ]
  });
}

function hr() {
  return new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "999999" } },
    spacing: { before: 200, after: 200 }
  });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

function coverageTable(groups) {
  const nameW = Math.round(CONTENT_WIDTH * 0.62);
  const limitW = CONTENT_WIDTH - nameW;
  const rows = [];

  rows.push(new TableRow({
    tableHeader: true,
    children: [
      new TableCell({
        width: { size: nameW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: BLUE },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 100, bottom: 100, left: 120, right: 120 },
        children: [new Paragraph({ children: [new TextRun({ text: "Coverage", bold: true, color: "FFFFFF", size: 26 })] })]
      }),
      new TableCell({
        width: { size: limitW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: BLUE },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 100, bottom: 100, left: 120, right: 120 },
        children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: "Limit", bold: true, color: "FFFFFF", size: 26 })] })]
      })
    ]
  }));

  groups.forEach((group, gi) => {
    rows.push(new TableRow({
      children: [
        new TableCell({
          width: { size: nameW, type: WidthType.DXA },
          columnSpan: 1,
          shading: { type: ShadingType.CLEAR, fill: "E3E8F5" },
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          children: [new Paragraph({ children: [new TextRun({ text: group.title, bold: true, size: 26, color: BLUE })] })]
        }),
        new TableCell({
          width: { size: limitW, type: WidthType.DXA },
          shading: { type: ShadingType.CLEAR, fill: "E3E8F5" },
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          children: [new Paragraph("")]
        })
      ]
    }));

    (group.items || []).forEach((item, idx) => {
      const fill = idx % 2 === 0 ? "FFFFFF" : LIGHT_GRAY;
      rows.push(new TableRow({
        children: [
          new TableCell({
            width: { size: nameW, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill },
            margins: { top: 90, bottom: 90, left: 220, right: 120 },
            children: [new Paragraph({ children: [new TextRun({ text: item.name, size: 25 })] })]
          }),
          new TableCell({
            width: { size: limitW, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill },
            margins: { top: 90, bottom: 90, left: 120, right: 120 },
            children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: item.limit || "—", size: 25 })] })]
          })
        ]
      }));
    });
  });

  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [nameW, limitW],
    rows
  });
}

function premiumTable(data) {
  const labelW = Math.round(CONTENT_WIDTH * 0.55);
  const valueW = CONTENT_WIDTH - labelW;
  const taxesAndFees = parseMoney(data.taxes_and_fees);
  const alreadyIncluded = data.required_initial_payment_includes_taxes_and_fees
    || data.required_initial_payment_includes_fee; // Backward-compatible with older data files.
  const initialPayment = alreadyIncluded
    ? data.required_initial_payment
    : addMoney(data.required_initial_payment, taxesAndFees);

  const rowsData = [
    ["Total Annual Premium", data.total_annual_premium || ""],
    ["Required Initial Payment", initialPayment],
    ...(taxesAndFees !== 0 ? [["Taxes and fees", "Included in initial payment"]] : []),
    ["Payment Plan", data.payment_plan_label || ""],
    ["Monthly Installment", data.monthly_installment || ""]
  ];

  const rows = rowsData.map(([label, value], idx) => new TableRow({
    children: [
      new TableCell({
        width: { size: labelW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: "FFF3B0" },
        margins: { top: 120, bottom: 120, left: 150, right: 120 },
        children: [new Paragraph({ children: [new TextRun({ text: label, bold: true, size: 28 })] })]
      }),
      new TableCell({
        width: { size: valueW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: "FFF3B0" },
        margins: { top: 120, bottom: 120, left: 150, right: 120 },
        children: [new Paragraph({ children: [new TextRun({ text: value, bold: true, size: 28 })] })]
      })
    ]
  }));

  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [labelW, valueW],
    rows
  });
}

// Generic "partner card": optional logo image, a blurb, and a list of
// [label, value] contact lines. Used for CNS, RTS/factoring, and any future partners.
function partnerCard({ title, logoFile, logoFallbackText, blurb, contactLines }) {
  const children = [];
  children.push(h2(title));
  const logoPath = logoFile ? path.join(ASSETS, logoFile) : null;
  if (logoPath && fs.existsSync(logoPath)) {
    children.push(new Paragraph({
      spacing: { before: 100, after: 200 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 260, height: 111 } })]
    }));
  } else if (logoFallbackText) {
    // Placeholder wordmark until the real logo file is supplied — swap in an ImageRun once available.
    children.push(new Paragraph({
      spacing: { before: 100, after: 200 },
      border: {
        top: { style: BorderStyle.SINGLE, size: 8, color: BLUE },
        bottom: { style: BorderStyle.SINGLE, size: 8, color: BLUE },
        left: { style: BorderStyle.SINGLE, size: 8, color: BLUE },
        right: { style: BorderStyle.SINGLE, size: 8, color: BLUE }
      },
      children: [new TextRun({ text: logoFallbackText, bold: true, size: 40, color: BLUE })]
    }));
  }
  if (blurb) children.push(bodyPara(blurb));
  (contactLines || []).forEach(([label, value]) => children.push(labeledBullet(label, value)));
  return children;
}

function partnersSection() {
  const children = [];

  children.push(...partnerCard({
    title: "Our Partners — Compliance",
    logoFile: "cns_logo.png",
    blurb: "We've partnered with CNS — Compliance Navigation Specialists — to help keep you DOT compliant and audit-ready: DOT audit support, driver qualification file management, drug & alcohol consortium administration, IFTA/IRP licensing and permitting, and more.",
    contactLines: [
      ["CNS Phone", "888.260.9448"],
      ["CNS Email", "info@cnsprotects.com"],
      ["CNS Website", "cnsprotects.com"]
    ]
  }));

  children.push(pageBreak());
  children.push(...partnerCard({
    title: "Our Partners — Factoring",
    logoFile: "rts_logo.png",
    logoFallbackText: "RTS FINANCIAL",
    blurb: "Need cash flow help between loads? We also work with factoring companies — ask us about it! Here's one of our trusted factoring partners, RTS Financial:",
    contactLines: [
      ["Contact", "Luke Johnson, Business Development Manager"],
      ["Phone", "(267) 814-2862"],
      ["Email", "lujohnson@rtsfinancial.com"]
    ]
  }));

  return children;
}

function buildDocument(data) {
  const logoPath = path.join(ASSETS, "logo.png");
  const logoImage = fs.existsSync(logoPath)
    ? new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 340, height: 88 } })
    : null;

  const children = [];

  // ---- Page 1: Cover ----
  children.push(h1("COMMERCIAL INSURANCE PROPOSAL"));
  if (logoImage) {
    children.push(new Paragraph({ spacing: { before: 100, after: 400 }, children: [logoImage] }));
  }
  children.push(labeledBullet("Policy Period", data.policy_period));
  children.push(new Paragraph({ spacing: { before: 300, after: 180 }, children: [new TextRun({ text: "PREPARED FOR:", bold: true, size: 30, color: BLUE })] }));
  children.push(labeledBullet("Business name", data.business_name));
  children.push(labeledBullet("Address", data.address));
  children.push(labeledBullet("Phone", data.phone));
  children.push(labeledBullet("Email", data.email));
  children.push(labeledBullet("Business Type", data.business_type));
  children.push(labeledBullet("USDOT Number", data.usdot_number));
  children.push(new Paragraph({ spacing: { before: 600 }, children: [] }));
  children.push(new Paragraph({ spacing: { before: 200, after: 80 }, children: [new TextRun({ text: "PRESENTED BY:", bold: true, size: 28, color: BLUE })] }));
  children.push(bodyPara("Jake Ferrara"));
  children.push(bodyPara("StreetSmart Insurance"));
  children.push(bodyPara("208 SOUTH STREET"));
  children.push(bodyPara("FREEHOLD, NJ 07728"));
  children.push(pageBreak());

  // ---- Page 2: Your Insurance Quote (video) ----
  const quoteVideoUrl = "https://www.streetsmart.insurance/quotevids/your-insurance-quote/";
  children.push(h1("COMMERCIAL INSURANCE PROPOSAL"));
  children.push(bodyPara(
    "Thank you for contacting me about your insurance needs. I am pleased to provide you with a quote from the many carriers we approached. We are able to make any changes you'd like to this proposal.",
    { size: 28 }
  ));
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 300, after: 300 },
    children: [new TextRun({ text: "Your Insurance Quote", bold: true, size: 44, color: SLATE })]
  }));
  const jakePhotoPath = path.join(ASSETS, "jake_photo.png");
  if (fs.existsSync(jakePhotoPath)) {
    children.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 100, after: 300 },
      children: [
        new ExternalHyperlink({
          link: quoteVideoUrl,
          children: [new ImageRun({ type: "png", data: fs.readFileSync(jakePhotoPath), transformation: { width: 620, height: 354 } })]
        })
      ]
    }));
  }
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 200, after: 200 },
    border: {
      top: { style: BorderStyle.SINGLE, size: 12, color: BLUE },
      bottom: { style: BorderStyle.SINGLE, size: 12, color: BLUE },
      left: { style: BorderStyle.SINGLE, size: 12, color: BLUE },
      right: { style: BorderStyle.SINGLE, size: 12, color: BLUE }
    },
    shading: { type: ShadingType.CLEAR, fill: "FFF3B0" },
    children: [
      new ExternalHyperlink({
        link: quoteVideoUrl,
        children: [new TextRun({ text: "CLICK HERE!", bold: true, size: 64, color: BLUE, underline: {}, highlight: "yellow" })]
      })
    ]
  }));
  children.push(pageBreak());

  // ---- Page 3: Why StreetSmart + Our Partners ----
  children.push(h2("Why do business with StreetSmart?"));
  children.push(highlightBullet("Text Alerts: ", "We send you text notifications for important account updates such as cancellation notices and upcoming renewals. You can also text us if you prefer."));
  children.push(subBullet([new TextRun({ text: "SAVE OUR NUMBER — text (732) 462-8343 to get started.", size: 28 })]));
  children.push(highlightBullet("Client Center: ", "Your one-stop shop to access your policy documents and make change requests."));
  children.push(subBullet([new TextRun({ text: "Get certificates of insurance 24/7/365. No passwords necessary!", size: 28 })]));
  children.push(highlightBullet("Contact Our Dedicated Service Team: ", "Prefer to speak with someone? No problem! You can contact our Service Team with any questions, concerns, or requests."));
  children.push(subBullet([new TextRun({ text: "The phone number is (732) 462-8343 (x1 for Personal Lines, x2 for Trucking, x3 for Commercial)", size: 28 })]));
  children.push(highlightBullet("Si Habla Español!", ""));
  children.push(highlightBullet("Renewals: ", "We have a dedicated Renewal Team that reviews your account every year to proactively assist you in reviewing any changes and coverage options available to you."));
  children.push(...partnersSection());
  children.push(pageBreak());

  // ---- Page 3: Section header ----
  children.push(h1("COMMERCIAL AUTO PROPOSAL"));
  children.push(new Paragraph({ spacing: { before: 200 }, children: [] }));

  // ---- Drivers & Operations ----
  children.push(h2("Drivers & Operations"));
  children.push(labeledBullet("Radius of Operation", data.radius_of_operation || "Not listed"));
  (data.rated_drivers || []).forEach((driver) => {
    const details = [
      driver.date_of_birth ? `DOB: ${driver.date_of_birth}` : "",
      driver.points !== undefined && driver.points !== "" ? `Points: ${driver.points}` : "",
      driver.additional_information || ""
    ].filter(Boolean).join(" | ");
    children.push(labeledBullet("Driver", `${driver.name || ""}${details ? ` — ${details}` : ""}`));
  });

  // ---- Coverage & Limits table ----
  children.push(h2("Coverages & Limits"));
  children.push(coverageTable(data.coverage_groups || []));
  children.push(pageBreak());

  // ---- Premium & Payment Terms ----
  children.push(hr());
  children.push(h2("Premium & Payment Terms"));
  children.push(premiumTable(data));
  children.push(hr());

  children.push(h2("Binding Requirements"));
  children.push(new Paragraph({
    spacing: { after: 160 },
    children: [new TextRun({ text: "To initiate coverage, the following must be submitted to the agent's office:", bold: true, size: 28, color: "C00000" })]
  }));
  children.push(redBullet("Signed Insurance Application (will be sent after we collect payment info)"));
  children.push(redBullet("Signed Underinsured/Uninsured Motorist Selection forms (will be sent after we collect payment info)"));
  children.push(redBullet("Electronic Funds Transfer (EFT) Authorization"));
  children.push(redBullet("Tax ID Number"));
  children.push(redBullet("Proof of Prior Insurance (Liability limits, expiration dates, and policy number)"));
  children.push(pageBreak());

  // ---- Legal Disclaimer ----
  children.push(h2("Legal Disclaimer"));
  children.push(new Paragraph({
    border: {
      top: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      bottom: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      left: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      right: { style: BorderStyle.SINGLE, size: 6, color: BLUE }
    },
    shading: { type: ShadingType.CLEAR, fill: "F5F8FF" },
    spacing: { before: 200, after: 200 },
    children: [new TextRun({
      text: "All quotes are subject to approval and change. No quotes are considered bound and issued unless written and confirmed by an agent. All taxes and fees are fully earned. POLICY COULD BE SUBJECT TO A 30 CANCELLATION CLAUSE SUBJECT TO THE FMCSA BMC-91X FILING!",
      size: 26, bold: true
    })]
  }));

  const doc = new Document({
    sections: [
      {
        properties: {
          page: {
            size: { width: PAGE_WIDTH, height: PAGE_HEIGHT },
            margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN }
          }
        },
        headers: { default: stripeHeader() },
        footers: { default: disclaimerFooter() },
        children
      }
    ]
  });

  return doc;
}

async function main() {
  const [, , dataPath, outPath] = process.argv;
  if (!dataPath || !outPath) {
    console.error("Usage: node generate_proposal.js data.json output.docx");
    process.exit(1);
  }
  const data = JSON.parse(fs.readFileSync(dataPath, "utf8"));
  const doc = buildDocument(data);
  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(outPath, buffer);
  console.log("Wrote", outPath);
}

main().catch((e) => { console.error(e); process.exit(1); });
