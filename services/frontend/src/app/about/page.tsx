import type { Metadata } from "next";
import Link from "next/link";
import { ScientificMethod } from "@/components/about/ScientificMethod";
import { ReusableKnowledge } from "@/components/about/ReusableKnowledge";
import "./about.css";

export const metadata: Metadata = {
  title: "About | REVEAL Mechanisms",
  description: "Explore how researcher context, AI agents and traceable scientific claims connect biomedical knowledge gaps to reusable data.",
};

export default function AboutPage() {
  return <main id="main" className="about-page">
    <nav className="page-topbar" aria-label="About navigation"><Link className="knowledge-gaps-back" href="/"><span aria-hidden="true">←</span> Knowledge gaps</Link></nav>
    <header className="about-intro">
      <h1>We are building a community of gap closers</h1>
      <p>Researchers bring biomedical questions. Agents help connect scientific claims to evidence, and evidence to data others can reuse.</p>
    </header>

    <section className="about-reuse" aria-labelledby="reuse-heading">
      <div className="about-section-heading">
        <h2 id="reuse-heading">Beyond FAIR: data with scientific context</h2>
        <p>Discover data through the questions it informs, the claims it tests and the people who make it useful.</p>
      </div>
      <ReusableKnowledge />
    </section>

    <section className="about-method" aria-labelledby="method-heading">
      <div className="about-section-heading">
        <h2 id="method-heading">The scientific method, connected</h2>
        <p>Your context connects biological ideas with the data that can test them.</p>
      </div>
      <ScientificMethod />
    </section>

    <footer className="about-footer">
      <div><h2>Bring your next question.</h2><p>Start with <a href="https://dismech.monarchinitiative.org/app/discussions/index.html" target="_blank" rel="noopener noreferrer">DisMech knowledge gaps</a> and data from the <a href="https://cfdeknowledge.org/r/kc_landing" target="_blank" rel="noopener noreferrer">CFDE Knowledge Center</a>.</p></div>
      <Link className="about-explore" href="/">Explore knowledge gaps <span aria-hidden="true">↗</span></Link>
    </footer>
  </main>;
}
