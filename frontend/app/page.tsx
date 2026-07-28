import Link from "next/link";

export default function Home() {
  return (
    <main className="max-w-2xl mx-auto py-24 px-6 text-center">
      <h1 className="text-3xl font-bold tracking-tight">SMTC Data Validation Framework</h1>
      <p className="text-slate-500 mt-3">V1 -- validation + violation reporting</p>
      <div className="flex gap-4 justify-center mt-8">
        <Link href="/dashboard" className="px-5 py-2.5 rounded-lg bg-blue-600 text-white font-medium">
          Dashboard
        </Link>
        <Link href="/rules" className="px-5 py-2.5 rounded-lg border border-slate-300 font-medium">
          Manage Rules
        </Link>
        <Link href="/runs" className="px-5 py-2.5 rounded-lg border border-slate-300 font-medium">
          Runs
        </Link>
      </div>
    </main>
  );
}
