import { Navigate, Route, Routes } from 'react-router-dom'
import { ToastProvider } from './components/Toast'
import TopBar from './components/TopBar'
import Dashboard from './pages/Dashboard'
import UpperDetail from './pages/UpperDetail'
import SummaryHistory from './pages/SummaryHistory'
import Research from './pages/Research'

export default function App() {
  return (
    <ToastProvider>
      <TopBar />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/upper/:uid" element={<UpperDetail />} />
        <Route path="/history" element={<SummaryHistory />} />
        <Route path="/research" element={<Research />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ToastProvider>
  )
}
