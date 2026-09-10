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
 *   "taxes_and_fees": "$450.00",                // StreetSmart default; use $0.00 only when Jake opts out
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
const NAVY = "0B2B6F";
const SLATE = "284969";
const PALE_BLUE = "EAF0FF";
const LIGHT_GRAY = "F6F8FC";

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

function taxesAndFeesAmount(data) {
  const value = data.taxes_and_fees;
  if (value === undefined || value === null || String(value).trim() === "") return 450;
  return parseMoney(value);
}

function clientInitialPayment(data) {
  const feeWasExplicitlyProvided = data.taxes_and_fees !== undefined
    && data.taxes_and_fees !== null
    && String(data.taxes_and_fees).trim() !== "";
  const alreadyIncluded = feeWasExplicitlyProvided && (
    data.required_initial_payment_includes_taxes_and_fees
    || data.required_initial_payment_includes_fee
  );
  return alreadyIncluded
    ? data.required_initial_payment
    : addMoney(data.required_initial_payment, taxesAndFeesAmount(data));
}

function coverDetail(label, value) {
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { after: 110 },
    children: [
      new TextRun({ text: `${label.toUpperCase()}  `, bold: true, size: 19, color: YELLOW }),
      new TextRun({ text: value || "", size: 24, color: "FFFFFF" })
    ]
  });
}

function coverHero(data, logoPath) {
  const content = [];
  if (fs.existsSync(logoPath)) {
    content.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { after: 500 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 360, height: 95 } })]
    }));
  }
  content.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { after: 70 },
    children: [new TextRun({ text: "COMMERCIAL INSURANCE", bold: true, size: 48, color: "FFFFFF" })]
  }));
  content.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { after: 420 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 22, color: YELLOW } },
    children: [new TextRun({ text: "QUOTE PROPOSAL", bold: true, size: 48, color: "FFFFFF" })]
  }));
  content.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { after: 80 },
    children: [new TextRun({ text: "PREPARED FOR", bold: true, size: 19, color: YELLOW })]
  }));
  content.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { after: 220 },
    children: [new TextRun({ text: data.business_name || "", bold: true, size: 38, color: "FFFFFF" })]
  }));
  content.push(coverDetail("Address", data.address));
  content.push(coverDetail("Business type", data.business_type));
  content.push(coverDetail("USDOT", data.usdot_number));
  content.push(coverDetail("Policy period", data.policy_period));
  content.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 500, after: 80 }, children: [new TextRun({ text: "PRESENTED BY", bold: true, size: 19, color: YELLOW })] }));
  content.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 }, children: [new TextRun({ text: "Jake Ferrara", bold: true, size: 28, color: "FFFFFF" })] }));
  content.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "StreetSmart Insurance", size: 22, color: "FFFFFF" })] }));
  content.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "208 South Street, Freehold, NJ 07728", size: 20, color: "FFFFFF" })] }));

  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: BLUE },
      margins: { top: 420, bottom: 420, left: 420, right: 420 },
      borders: {
        top: { style: BorderStyle.SINGLE, size: 18, color: YELLOW },
        bottom: { style: BorderStyle.SINGLE, size: 18, color: YELLOW },
        left: { style: BorderStyle.NONE, size: 0, color: BLUE },
        right: { style: BorderStyle.NONE, size: 0, color: BLUE }
      },
      children: content
    })] })]
  });
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
  const logoPath = path.join(ASSETS, "logo-expanded-v2.png");
  const logo = fs.existsSync(logoPath)
    ? new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 230, height: 61 } })
    : null;
  const leftW = Math.round(CONTENT_WIDTH * 0.42);
  const rightW = CONTENT_WIDTH - leftW;
  return new Header({
    children: [
      new Table({
        width: { size: CONTENT_WIDTH, type: WidthType.DXA },
        columnWidths: [leftW, rightW],
        rows: [new TableRow({ children: [
          new TableCell({
            width: { size: leftW, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: BLUE },
            verticalAlign: VerticalAlign.CENTER,
            margins: { top: 100, bottom: 100, left: 120, right: 80 },
            borders: { bottom: { style: BorderStyle.SINGLE, size: 18, color: YELLOW } },
            children: [new Paragraph({ alignment: AlignmentType.CENTER, children: logo ? [logo] : [] })]
          }),
          new TableCell({
            width: { size: rightW, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: BLUE },
            verticalAlign: VerticalAlign.CENTER,
            margins: { top: 100, bottom: 100, left: 80, right: 120 },
            borders: { bottom: { style: BorderStyle.SINGLE, size: 18, color: YELLOW } },
            children: [new Paragraph({
              alignment: AlignmentType.RIGHT,
              children: [new TextRun({ text: "COMMERCIAL INSURANCE", bold: true, color: "FFFFFF", size: 21 })]
            })]
          })
        ] })]
      })
    ]
  });
}

function h1(text) {
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 150, bottom: 140, left: 180, right: 180 },
      borders: { bottom: { style: BorderStyle.SINGLE, size: 20, color: YELLOW } },
      children: [new Paragraph({ children: [new TextRun({ text, bold: true, size: 36, color: "FFFFFF" })] })]
    })] })]
  });
}

function h2(text) {
  return new Paragraph({
    spacing: { before: 320, after: 180 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 14, color: YELLOW } },
    children: [new TextRun({ text, bold: true, size: 32, color: NAVY })]
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

function findCoverageLimit(groups, pattern) {
  for (const group of groups || []) {
    for (const item of group.items || []) {
      if (pattern.test(`${group.title} ${item.name}`)) return item.limit || "Not listed";
    }
  }
  return "Not listed";
}

function summaryCard(label, value, featured = false) {
  return new TableCell({
    width: { size: Math.round(CONTENT_WIDTH / 2), type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: featured ? NAVY : PALE_BLUE },
    verticalAlign: VerticalAlign.CENTER,
    margins: { top: 180, bottom: 180, left: 190, right: 190 },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 10, color: featured ? YELLOW : "CCD8F5" },
      bottom: { style: BorderStyle.SINGLE, size: 10, color: featured ? YELLOW : "CCD8F5" },
      left: { style: BorderStyle.SINGLE, size: 10, color: featured ? YELLOW : "CCD8F5" },
      right: { style: BorderStyle.SINGLE, size: 10, color: featured ? YELLOW : "CCD8F5" }
    },
    children: [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 70 },
        children: [new TextRun({ text: label.toUpperCase(), bold: true, size: 18, color: featured ? YELLOW : BLUE })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: value || "Not listed", bold: true, size: 29, color: featured ? "FFFFFF" : NAVY })]
      })
    ]
  });
}

function quoteAtGlance(data) {
  const coverages = data.coverage_groups || [];
  const liability = findCoverageLimit(coverages, /bodily injury|property damage liability/i);
  const cargo = findCoverageLimit(coverages, /motor truck cargo/i);
  const driverSummary = (data.rated_drivers || []).length
    ? `${data.rated_drivers.length} rated driver${data.rated_drivers.length === 1 ? "" : "s"}`
    : "Not listed";
  const values = [
    ["Total Annual Premium", data.total_annual_premium || "Not listed", true],
    ["Initial Payment", clientInitialPayment(data) || "Not listed", true],
    ["Monthly Payment", data.monthly_installment || "Not listed", false],
    ["Auto Liability", liability, false],
    ["Motor Truck Cargo", cargo, false],
    ["Radius / Drivers", `${data.radius_of_operation || "Radius not listed"} | ${driverSummary}`, false]
  ];
  const rows = [];
  for (let i = 0; i < values.length; i += 2) {
    rows.push(new TableRow({ children: [summaryCard(...values[i]), summaryCard(...values[i + 1])] }));
  }
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [Math.round(CONTENT_WIDTH / 2), Math.round(CONTENT_WIDTH / 2)],
    rows
  });
}

function fitPoint(title, text) {
  return new Paragraph({
    spacing: { after: 150 },
    border: { left: { style: BorderStyle.SINGLE, size: 16, color: YELLOW } },
    indent: { left: 180 },
    children: [
      new TextRun({ text: `${title}: `, bold: true, size: 27, color: NAVY }),
      new TextRun({ text, size: 27, color: SLATE })
    ]
  });
}

const COVERAGE_EXPLANATIONS = {
  "Commercial Automobile Liability": "Helps protect the business against covered bodily injury and property-damage claims arising from scheduled vehicle operations.",
  "Automobile Physical Damage": "Helps protect scheduled vehicles against covered collision and non-collision damage, subject to the listed deductibles.",
  "Motor Truck Cargo": "Helps protect covered property being transported, subject to policy terms, limits, and deductibles.",
  "Excess Auto Liability": "Provides an additional layer of liability protection above the listed underlying auto liability."
};

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
          children: [new Paragraph({ children: [
            new TextRun({ text: group.title, bold: true, size: 26, color: BLUE }),
            ...(COVERAGE_EXPLANATIONS[group.title] ? [
              new TextRun({ text: `\n${COVERAGE_EXPLANATIONS[group.title]}`, italics: true, size: 19, color: SLATE })
            ] : [])
          ] })]
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
  const taxesAndFees = taxesAndFeesAmount(data);
  const initialPayment = clientInitialPayment(data);

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
        shading: { type: ShadingType.CLEAR, fill: idx === 0 ? YELLOW : NAVY },
        margins: { top: 150, bottom: 150, left: 170, right: 120 },
        children: [new Paragraph({ children: [new TextRun({ text: label, bold: true, size: 28, color: idx === 0 ? NAVY : "FFFFFF" })] })]
      }),
      new TableCell({
        width: { size: valueW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: idx === 0 ? "FFF3B0" : PALE_BLUE },
        margins: { top: 150, bottom: 150, left: 170, right: 120 },
        children: [new Paragraph({ children: [new TextRun({ text: value, bold: true, size: idx === 0 ? 32 : 28, color: NAVY })] })]
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
      alignment: AlignmentType.CENTER,
      spacing: { before: 140, after: 240 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 360, height: 154 } })]
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
  if (blurb) children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 210, bottom: 210, left: 220, right: 220 },
      borders: { top: { style: BorderStyle.SINGLE, size: 12, color: YELLOW }, bottom: { style: BorderStyle.SINGLE, size: 12, color: YELLOW } },
      children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: blurb, size: 25, color: "FFFFFF" })] })]
    })] })]
  }));
  if ((contactLines || []).length) {
    children.push(h2("Partner Contact"));
    const cardW = Math.floor(CONTENT_WIDTH / contactLines.length);
    children.push(new Table({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      columnWidths: contactLines.map((_, i) => i === contactLines.length - 1 ? CONTENT_WIDTH - cardW * (contactLines.length - 1) : cardW),
      rows: [new TableRow({ children: contactLines.map(([label, value]) => new TableCell({
        shading: { type: ShadingType.CLEAR, fill: PALE_BLUE },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 170, bottom: 170, left: 130, right: 130 },
        borders: { top: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" }, bottom: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" }, left: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" }, right: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" } },
        children: [
          new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 70 }, children: [new TextRun({ text: label.toUpperCase(), bold: true, size: 19, color: BLUE })] }),
          new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: value, bold: true, size: 22, color: NAVY })] })
        ]
      })) })]
    }));
  }
  return children;
}

function partnersSection() {
  const children = [];

  children.push(...compliancePartnerPage());

  children.push(pageBreak());
  children.push(...factoringPartnerPage());

  return children;
}

function compliancePartnerPage() {
  const children = [h1("SAFETY & COMPLIANCE SUPPORT")];
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 240, bottom: 240, left: 240, right: 240 },
      borders: { bottom: { style: BorderStyle.SINGLE, size: 18, color: YELLOW } },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 }, children: [new TextRun({ text: "DOT COMPLIANCE GROUP", bold: true, size: 42, color: "FFFFFF" })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "A dedicated resource for safety and compliance support.", size: 27, color: "FFFFFF" })] })
      ]
    })] })]
  }));
  children.push(h2("Your Compliance Contact"));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [Math.round(CONTENT_WIDTH * 0.28), Math.round(CONTENT_WIDTH * 0.72)],
    rows: [
      ["Contact", "LaShunda Wiggs"],
      ["Title", "Compliance Account Specialist"],
      ["Agent ID", "115155"],
      ["Email", "lashunda@dotcompliancegroup.com"],
      ["Direct", "(817) 769-7938"],
      ["Office", "(972) 703-5228"],
      ["Website", "dotcompliancegroup.com"]
    ].map(([label, value], idx) => new TableRow({ children: [
      new TableCell({
        shading: { type: ShadingType.CLEAR, fill: NAVY },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 125, bottom: 125, left: 150, right: 150 },
        children: [new Paragraph({ children: [new TextRun({ text: label, bold: true, size: 24, color: "FFFFFF" })] })]
      }),
      new TableCell({
        shading: { type: ShadingType.CLEAR, fill: idx % 2 === 0 ? PALE_BLUE : LIGHT_GRAY },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 125, bottom: 125, left: 170, right: 150 },
        children: [new Paragraph({ children: [new TextRun({ text: value, bold: true, size: 24, color: NAVY })] })]
      })
    ] }))
  }));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: PALE_BLUE },
      margins: { top: 180, bottom: 180, left: 190, right: 190 },
      borders: { left: { style: BorderStyle.SINGLE, size: 14, color: YELLOW } },
      children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Contact LaShunda for assistance with your safety and compliance needs.", bold: true, size: 25, color: SLATE })] })]
    })] })]
  }));
  return children;
}

function factoringPartnerPage() {
  const children = [h1("FACTORING SUPPORT")];
  const logoPath = path.join(ASSETS, "rts_logo.png");
  const brandChildren = logoPath && fs.existsSync(logoPath)
    ? [new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 260, height: 111 } })]
    : [new TextRun({ text: "RTS FINANCIAL", bold: true, size: 42, color: "FFFFFF" })];
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 220, bottom: 220, left: 240, right: 240 },
      borders: { bottom: { style: BorderStyle.SINGLE, size: 18, color: YELLOW } },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 }, children: brandChildren }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "A trusted resource when your business needs stronger cash flow between loads.", size: 27, color: "FFFFFF" })] })
      ]
    })] })]
  }));
  children.push(h2("How Factoring Can Help"));
  const benefits = [
    ["FASTER CASH FLOW", "Turn eligible invoices into working capital sooner."],
    ["MORE FLEXIBILITY", "Cover fuel, payroll, and operating expenses between customer payments."],
    ["BUSINESS SUPPORT", "Keep your trucks moving while receivables are still outstanding."]
  ];
  const cardW = Math.floor(CONTENT_WIDTH / 3);
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [cardW, cardW, CONTENT_WIDTH - cardW * 2],
    rows: [new TableRow({ children: benefits.map(([title, text]) => new TableCell({
      width: { size: cardW, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: PALE_BLUE },
      verticalAlign: VerticalAlign.CENTER,
      margins: { top: 190, bottom: 190, left: 150, right: 150 },
      borders: {
        top: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" },
        bottom: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" },
        left: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" },
        right: { style: BorderStyle.SINGLE, size: 8, color: "CCD8F5" }
      },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 90 }, children: [new TextRun({ text: title, bold: true, size: 21, color: BLUE })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text, size: 22, color: SLATE })] })
      ]
    })) })]
  }));
  children.push(h2("Your RTS Contact"));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [Math.round(CONTENT_WIDTH * 0.28), Math.round(CONTENT_WIDTH * 0.72)],
    rows: [
      ["Contact", "Drew Munoz, Business Development Representative"],
      ["Office", "(267) 814-2874"],
      ["Email", "dmunoz@rtsinc.com"],
      ["Website", "www.rtsinc.com"]
    ].map(([label, value]) => new TableRow({ children: [
      new TableCell({ shading: { type: ShadingType.CLEAR, fill: NAVY }, margins: { top: 130, bottom: 130, left: 150, right: 150 }, children: [new Paragraph({ children: [new TextRun({ text: label, bold: true, size: 25, color: "FFFFFF" })] })] }),
      new TableCell({ shading: { type: ShadingType.CLEAR, fill: LIGHT_GRAY }, margins: { top: 130, bottom: 130, left: 170, right: 150 }, children: [new Paragraph({ children: [new TextRun({ text: value, bold: true, size: 25, color: NAVY })] })] })
    ] }))
  }));
  children.push(bodyPara("Ask StreetSmart to make an introduction if you would like to explore factoring options.", { italics: true, color: SLATE }));
  return children;
}

function buildDocument(data) {
  const logoPath = path.join(ASSETS, "logo-expanded-v2.png");
  const logoImage = fs.existsSync(logoPath)
    ? new ImageRun({ type: "png", data: fs.readFileSync(logoPath), transformation: { width: 340, height: 88 } })
    : null;

  const children = [];

  // ---- Page 1: Cover ----
  children.push(coverHero(data, logoPath));
  children.push(pageBreak());

  // ---- Page 2: Quote at a Glance ----
  children.push(h1("YOUR QUOTE AT A GLANCE"));
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 180, after: 220 },
    children: [new TextRun({ text: "Smart coverage for the road ahead.", bold: true, size: 31, color: SLATE })]
  }));
  children.push(quoteAtGlance(data));
  children.push(h2("Why This Quote Fits Your Business"));
  children.push(fitPoint("Built for your operation", `Structured around your ${data.business_type || "commercial trucking"} business.`));
  children.push(fitPoint("Meaningful liability protection", `Includes ${findCoverageLimit(data.coverage_groups || [], /bodily injury|property damage liability/i)} in quoted commercial auto liability protection.`));
  children.push(fitPoint("Cargo protection on the road", `Includes ${findCoverageLimit(data.coverage_groups || [], /motor truck cargo/i)} in quoted motor truck cargo protection.`));
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100 },
    children: [new TextRun({ text: "Coverage is subject to policy terms, conditions, exclusions, and carrier approval.", italics: true, size: 18, color: "666666" })]
  }));
  children.push(pageBreak());

  // ---- Page 3: Your Insurance Quote (video) ----
  const quoteVideoUrl = "https://www.streetsmart.insurance/quotevids/your-insurance-quote/";
  children.push(h1("A PERSONAL WALKTHROUGH OF YOUR QUOTE"));
  children.push(bodyPara(
    "Thank you for contacting me about your insurance needs. I recorded a quick walkthrough to explain the proposal, highlight the key coverage, and help you understand the payment options.",
    { size: 28 }
  ));
  const jakePhotoPath = path.join(ASSETS, "jake_photo.png");
  const qrPath = path.join(ASSETS, "quote-video-qr.png");
  const videoLeftW = Math.round(CONTENT_WIDTH * 0.67);
  const videoRightW = CONTENT_WIDTH - videoLeftW;
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [videoLeftW, videoRightW],
    rows: [new TableRow({ children: [
      new TableCell({
        width: { size: videoLeftW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: LIGHT_GRAY },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 150, bottom: 150, left: 150, right: 150 },
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: fs.existsSync(jakePhotoPath) ? [new ExternalHyperlink({ link: quoteVideoUrl, children: [new ImageRun({ type: "png", data: fs.readFileSync(jakePhotoPath), transformation: { width: 470, height: 269 } })] })] : []
        })]
      }),
      new TableCell({
        width: { size: videoRightW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: NAVY },
        verticalAlign: VerticalAlign.CENTER,
        margins: { top: 170, bottom: 170, left: 150, right: 150 },
        children: [
          new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 130 }, children: [new TextRun({ text: "SCAN TO WATCH", bold: true, size: 23, color: YELLOW })] }),
          new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 }, children: fs.existsSync(qrPath) ? [new ExternalHyperlink({ link: quoteVideoUrl, children: [new ImageRun({ type: "png", data: fs.readFileSync(qrPath), transformation: { width: 142, height: 142 } })] })] : [] }),
          new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Use your phone camera", size: 19, color: "FFFFFF" })] })
        ]
      })
    ] })]
  }));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: YELLOW },
      margins: { top: 190, bottom: 190, left: 180, right: 180 },
      borders: { top: { style: BorderStyle.SINGLE, size: 10, color: NAVY }, bottom: { style: BorderStyle.SINGLE, size: 10, color: NAVY } },
      children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ExternalHyperlink({ link: quoteVideoUrl, children: [new TextRun({ text: "WATCH JAKE EXPLAIN YOUR QUOTE", bold: true, size: 34, color: NAVY, underline: {} })] })]
      })]
    })] })]
  }));
  children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 220 }, children: [new TextRun({ text: "Questions after watching? Call or text Jake at (732) 462-8343.", bold: true, size: 27, color: SLATE })] }));
  children.push(pageBreak());

  // ---- Page 4: Why StreetSmart + Our Partners ----
  children.push(h2("Why do business with StreetSmart?"));
  children.push(highlightBullet("Text Alerts: ", "We send you text notifications for important account updates such as cancellation notices and upcoming renewals. You can also text us if you prefer."));
  children.push(subBullet([new TextRun({ text: "SAVE OUR NUMBER — text (732) 462-8343 to get started.", size: 28 })]));
  children.push(highlightBullet("Client Center: ", "Your one-stop shop to access your policy documents and make change requests."));
  children.push(subBullet([new TextRun({ text: "Get certificates of insurance 24/7/365. No passwords necessary!", size: 28 })]));
  children.push(highlightBullet("Contact Our Dedicated Service Team: ", "Prefer to speak with someone? No problem! You can contact our Service Team with any questions, concerns, or requests."));
  children.push(subBullet([new TextRun({ text: "The phone number is (732) 462-8343 (x1 for Personal Lines, x2 for Trucking, x3 for Commercial)", size: 28 })]));
  children.push(highlightBullet("Si Habla Español!", ""));
  children.push(highlightBullet("Renewals: ", "We have a dedicated Renewal Team that reviews your account every year to proactively assist you in reviewing any changes and coverage options available to you."));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_WIDTH, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 210, bottom: 210, left: 220, right: 220 },
      borders: { top: { style: BorderStyle.SINGLE, size: 12, color: YELLOW }, bottom: { style: BorderStyle.SINGLE, size: 12, color: YELLOW } },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 70 }, children: [new TextRun({ text: "THE STREETSMART DIFFERENCE", bold: true, size: 27, color: YELLOW })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Responsive service, proactive renewal support, and trucking-focused resources - all in one place.", size: 25, color: "FFFFFF" })] })
      ]
    })] })]
  }));
  children.push(pageBreak());
  children.push(...partnersSection());
  children.push(pageBreak());

  // ---- Coverage section ----
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
  // Start the payment section on a fresh page without inserting a trailing
  // break paragraph that can create a blank page when the coverage table
  // already reaches the bottom of the preceding page.
  children.push(new Paragraph({ pageBreakBefore: true, children: [] }));

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
  children.push(h1("READY TO MOVE FORWARD?"));
  children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 220, after: 220 }, children: [new TextRun({ text: "Three simple steps can get your coverage moving.", bold: true, size: 31, color: SLATE })] }));
  const steps = [
    ["1", "APPROVE THE QUOTE", "Tell Jake you are ready to proceed."],
    ["2", "COMPLETE PAYMENT", "Provide the information needed for the initial payment."],
    ["3", "SIGN & SUBMIT", "Complete the required applications and authorization forms."]
  ];
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [Math.round(CONTENT_WIDTH / 3), Math.round(CONTENT_WIDTH / 3), CONTENT_WIDTH - Math.round(CONTENT_WIDTH / 3) * 2],
    rows: [new TableRow({ children: steps.map(([number, title, text]) => new TableCell({
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      verticalAlign: VerticalAlign.CENTER,
      margins: { top: 190, bottom: 190, left: 150, right: 150 },
      borders: { top: { style: BorderStyle.SINGLE, size: 10, color: YELLOW }, bottom: { style: BorderStyle.SINGLE, size: 10, color: YELLOW }, left: { style: BorderStyle.SINGLE, size: 10, color: YELLOW }, right: { style: BorderStyle.SINGLE, size: 10, color: YELLOW } },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 }, children: [new TextRun({ text: number, bold: true, size: 44, color: YELLOW })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 }, children: [new TextRun({ text: title, bold: true, size: 21, color: "FFFFFF" })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text, size: 21, color: "FFFFFF" })] })
      ]
    })) })]
  }));
  children.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    rows: [new TableRow({ children: [new TableCell({
      shading: { type: ShadingType.CLEAR, fill: PALE_BLUE },
      margins: { top: 180, bottom: 180, left: 190, right: 190 },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 70 }, children: [new TextRun({ text: "JAKE FERRARA | STREETSMART INSURANCE", bold: true, size: 27, color: NAVY })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 70 }, children: [new TextRun({ text: "Call or text (732) 462-8343", bold: true, size: 25, color: BLUE })] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Smart coverage for the road ahead.", italics: true, size: 24, color: SLATE })] })
      ]
    })] })]
  }));
  children.push(h2("Legal Disclaimer"));
  children.push(new Paragraph({
    border: {
      top: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      bottom: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      left: { style: BorderStyle.SINGLE, size: 6, color: BLUE },
      right: { style: BorderStyle.SINGLE, size: 6, color: BLUE }
    },
    shading: { type: ShadingType.CLEAR, fill: NAVY },
    spacing: { before: 200, after: 200 },
    children: [new TextRun({
      text: "All quotes are subject to approval and change. No quotes are considered bound and issued unless written and confirmed by an agent. All taxes and fees are fully earned. POLICY COULD BE SUBJECT TO A 30 CANCELLATION CLAUSE SUBJECT TO THE FMCSA BMC-91X FILING!",
      size: 26, bold: true, color: "FFFFFF"
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
