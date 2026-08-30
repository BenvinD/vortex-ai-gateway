# vortex-ai-gateway

**v0.2**

## What is This?

vortex-ai-gateway is an AI Gateway designed to serve as a unified control plane and routing hub for AI model interactions. It manages request flow, handles authentication, orchestrates model selection, and provides a centralized entry point for AI applications.

## About the Name

**Vortex** — A vortex represents a center of concentrated activity and convergence. In fluid dynamics, a vortex is where multiple flows merge into a cohesive center. We chose this name because a gateway should act as a convergence point—drawing together multiple AI requests, models, and services, then directing them intelligently through a unified system.

**AI Gateway** — Self-explanatory. A gateway in networking and systems design controls and directs traffic between different domains. An AI Gateway specifically manages traffic and interactions with artificial intelligence systems.

Together, **vortex-ai-gateway** evokes both the convergence point metaphor and the explicit purpose of the system.

## Getting Started

### Installation

```bash
# Install dependencies
pip install -r requirements.txt
```

### Running the Application

```bash
# Start the gateway server
uvicorn src.gateway:app --reload
```

The server will be available at `http://localhost:8000`

### Testing

```bash
# Run all tests
pytest tests/ -v

# Run tests with coverage report
pytest tests/ -v --cov=src --cov-report=html
```

## CI/CD

This repository uses GitHub Actions for continuous integration. Every push and pull request triggers:

- **Test Suite**: Python 3.10, 3.11, 3.12 compatibility testing
- **Code Quality**: Basic linting with flake8
- **Coverage**: Automated coverage tracking

The `main` branch is protected and requires:
- ✅ All CI checks to pass
- ✅ At least 1 pull request review
- ✅ No force pushes or deletions

## License

Licensed under the Apache License 2.0. See LICENSE file for details.