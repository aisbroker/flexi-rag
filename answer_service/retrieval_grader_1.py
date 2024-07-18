### Retrieval Grader

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_openai import ChatOpenAI

from index_service.build_index import vectorStoreRetriever

def get_relevant_documents(question):
    """Get relevant documents for a given question."""

    #question = "agent memory"
    #question = "What is Java?"

    # Data model
    class GradeDocuments(BaseModel):
        """Binary score for relevance check on retrieved documents."""

        binary_score: str = Field(
            description="Documents are relevant to the question, 'yes' or 'no'"
        )


    # LLM with function call
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0) # cheaper that gpt-4o
    structured_llm_grader = llm.with_structured_output(GradeDocuments)

    # Prompt
    system = """You are a grader assessing relevance of a retrieved document to a user question. \n 
        If the document contains keyword(s) or semantic meaning related to the user question, grade it as relevant. \n
        It does not need to be a stringent test. The goal is to filter out erroneous retrievals. \n
        Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question.\n
        Just return 'yes' or 'no' as the answer. \n"""
    grade_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("human", "Retrieved document: \n\n {document} \n\n User question: {question}"),
        ]
    )

    retrieval_grader = grade_prompt | structured_llm_grader


    #docs = retriever.get_relevant_documents(question) # LangChainDeprecationWarning: The method `BaseRetriever.get_relevant_documents` was deprecated in langchain-core 0.1.46 and will be removed in 0.3.0. Use invoke instead.
    #docs = retriever.invoke({"question": question, "documents": docs})
    global vectorStoreRetriever
    docs = vectorStoreRetriever.invoke(input=question)
    print("found "+str(len(docs))+" docs in vectorstore")

    # iterate over the documents
    relevant_docs = []
    for doc in docs:
        # print(doc.page_content)
        doc_txt = doc.page_content
        relevance_binary_score = retrieval_grader.invoke({"question": question, "document": doc_txt})
        print(relevance_binary_score)
        if (relevance_binary_score.binary_score == "yes"):
            relevant_docs.append(doc)
    print("found "+str(len(relevant_docs))+" relevant docs")
    return relevant_docs
