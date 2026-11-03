// Minimal, safe inline markdown: **bold**, _italic_ (no HTML injection).
import React from 'react'

export default function Md({ text }) {
  const parts = String(text ?? '').split(/(\*\*[^*]+\*\*|_[^_]+_)/g)
  return (
    <>
      {parts.map((p, i) => {
        if (p.startsWith('**') && p.endsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>
        if (p.startsWith('_') && p.endsWith('_') && p.length > 2) return <em key={i}>{p.slice(1, -1)}</em>
        return <React.Fragment key={i}>{p}</React.Fragment>
      })}
    </>
  )
}
