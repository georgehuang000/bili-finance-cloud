import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, HashRouter } from 'react-router-dom'
import App from './App.jsx'
import { READ_ONLY, AccessProvider } from './access'
import './styles.css'
import './research.css'

const Router = READ_ONLY ? HashRouter : BrowserRouter

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Router>
      <AccessProvider><App /></AccessProvider>
    </Router>
  </React.StrictMode>,
)
