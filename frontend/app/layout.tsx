import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "SMTC Data Validation Framework",
  description: "Data quality validation and violation reporting",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-slate-50 text-slate-900 min-h-screen">{children}</body>
    </html>
  );
}
